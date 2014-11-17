"""Sequence generation framework"""

import copy
import logging
from abc import abstractmethod

from theano import tensor

from blocks.bricks import (
    Brick, MLP, Identity,
    DefaultRNG, lazy, application)
from blocks.recurrent import recurrent
from blocks.lookup import LookupTable
from blocks.utils import dict_union

logger = logging.getLogger(__name__)


class BaseSequenceGenerator(Brick):
    """A generic sequence generator.

    This class combines two components, a readout network and an
    attention-equipped recurrent transition, into a context-dependent
    sequence generator. The generation algorithm description follows.

    *Definitions:*

    * states of the generator are the states of the transition as specified
      by its `apply` method signature

    * contexts of the generator are the contexts of the transition as specified
      in its `apply` method signature

    * glimpses are intermediate entities computed from the states, the contexts
      and the previous step glimpses. They are computed by in the transition's
      `apply` method when not given or by explicitly calling the transition's
      `take_look` method. The output names of `take_look` specify the set of
      glimpses taken by the generator.

    *Algorithm:*

    0. The initial states are computed from the contexts. The transition
       signature is assumed to contain the initialization logic. Fake previous
       outputs and fake previous glimpses are created using `initial_outputs`
       and `initial_glimpses` methods of the readout and the transition
       respectively.

    1. Given the contexts, the current state and glimpses from the previous
       step the attention mechanism hidden in the transition produces current
       step glimpses. This happens in the `take_look` method of the transition.

    2. Using the contexts, the fed back output from the previous step,
       the current states and glimpses, the readout brick is used to generate
       the new output by calling its `readout` and `emit` methods.

    3. The new output is fed back in the `feedback` method of the readout
       brick. This feedback, together with the contexts, the glimpses and
       the previous states is used to get the new states in the transition's
       `apply` method.

    4. Back to step 1, if desired sequence length is not yet reached.

    *Notes:*

    * For machine translation we would have only one glimpse: the weighted
      average of the annotations.

    * For speech recognition we would have three: the weighted average,
      the alignment and the monotonicity penalty.

    Parameters
    ----------
        readout : a subclass of AbstractReadout
            The readout component of the sequence generator.
        transition : a subclass of AbstractAttentionTransition
            The transition component of the sequence generator.

    """
    @lazy
    def __init__(self, readout, fork, transition,
                 weights_init=None, biases_init=None, **kwargs):
        super(BaseSequenceGenerator, self).__init__(**kwargs)
        self.__dict__.update(**locals())
        del self.self

        self.state_names = transition.state_names
        self.context_names = transition.context_names
        self.glimpse_names = transition.glimpse_names
        self.children = [self.readout, self.fork, self.transition]

    def _push_allocation_config(self):
        # Configure readout
        # TODO: optional states? contexts?
        state_dims = {name: self.transition.get_dim(name)
                      for name in self.state_names}
        context_dims = {name: self.transition.get_dim(name)
                        for name in self.context_names}
        self.glimpse_dims = {name: self.transition.get_dim(name)
                             for name in self.glimpse_names}
        self.readout.source_dims = dict_union(
            state_dims, context_dims, self.glimpse_dims)

        # Configure fork
        feedback_names = self.readout.feedback.outputs
        assert len(feedback_names) == 1
        self.fork.input_dim = self.readout.get_dim(feedback_names[0])
        self.fork.fork_dims = {
            name: self.transition.get_dim(name)
            for name in self.fork.apply.outputs}

    def _push_initialization_config(self):
        for brick in self.children:
            if self.weights_init:
                brick.weights_init = self.weights_init
            if self.biases_init:
                brick.biases_init = self.weights_init

    @application
    def cost(self, outputs, mask=None, **kwargs):
        """Returns generation costs for output sequences.

        Parameters
        1----------
        outputs : Theano variable
            The 3(2) dimensional tensor containing output sequences.
            The dimension 0 must stand for time, the dimension 1 for the
            position on the batch.
        mask : The 0/1 matrix identifying fake outputs.

        Notes
        -----
        The contexts are expected as keyword arguments.

        """
        batch_size = outputs.shape[-2]

        # Prepare input for the iterative part
        states = {name: kwargs[name] for name in self.state_names
                  if name in kwargs}
        contexts = {name: kwargs[name] for name in self.context_names}
        feedback = self.readout.feedback(outputs)
        inputs = (self.fork.apply(feedback, return_dict=True)
            if self.fork
            else {'feedback' : feedback})

        # Run the recurrent network
        results = self.transition.apply(
            mask=mask, iterate=True,
            return_initial_states=True, return_dict=True,
            **dict_union(inputs, states, contexts))

        # Separate the deliverables
        states = {name: results[name][:-1] for name in self.state_names}
        glimpses = {name: results[name] for name in self.glimpse_names}

        # Compute the cost
        feedback = tensor.roll(feedback, 1, 0)
        feedback = tensor.set_subtensor(
            feedback[0],
            self.readout.feedback(self.readout.initial_outputs(
                batch_size, **contexts)))
        readouts = self.readout.readout(
            feedback=feedback, **dict_union(states, glimpses, contexts))
        costs = self.readout.cost(readouts, outputs)

        # In case the user needs some glimpses or states or smth else
        also_return = kwargs.get("also_return")
        if also_return:
            others = {name: results[name] for name in also_return}
            return (costs, others)
        return costs

    @recurrent
    def generate(self, outputs, **kwargs):
        """A sequence generation step.

        Parameters
        ----------
        outputs : Theano variable
            The outputs from the previous step.

        Notes
        -----
            The contexts, previous states and glimpses are expected
            as keyword arguments.

        """

        states = {name: kwargs[name] for name in self.state_names}
        contexts = {name: kwargs[name] for name in self.context_names}
        glimpses = {name: kwargs[name] for name in self.glimpse_names}

        next_glimpses = self.transition.take_look(
            return_dict=True,
            **dict_union(states, glimpses, contexts))
        next_readouts = self.readout.readout(
            feedback=self.readout.feedback(outputs),
            **dict_union(states, next_glimpses, contexts))
        next_outputs = self.readout.emit(next_readouts)
        next_costs = self.readout.cost(next_readouts, next_outputs)
        next_feedback = self.readout.feedback(next_outputs)
        next_inputs = (self.fork.apply(next_feedback, return_dict=True)
            if self.fork else {'feedback' : next_feedback})
        next_states = self.transition.apply(
             return_list=True, iterate=False,
            **dict_union(next_inputs, states, glimpses, contexts))
        return (next_states + [next_outputs]
                + next_glimpses.values() + [next_costs])

    @generate.delegate
    def generate_delegate(self):
        return self.transition.apply

    @generate.property('states')
    def generate_states(self):
        return self.state_names + ['outputs'] + self.glimpse_names

    @generate.property('outputs')
    def generate_outputs(self):
        return (self.state_names + ['outputs']
                + self.glimpse_names + ['costs'])

    def get_dim(self, name):
        if name in self.state_names + self.context_names + self.glimpse_names:
            return self.transition.get_dim(name)
        elif name == 'outputs':
            return self.readout.get_dim(name)
        return super(BaseSequenceGenerator, self).get_dim(name)

    def initial_state(self, name, batch_size, *args, **kwargs):
        if name == 'outputs':
            return self.readout.initial_outputs(batch_size)
        elif name in self.state_names + self.glimpse_names:
            return self.transition.initial_state(name, batch_size,
                                                 *args, **kwargs)
        else:
            # TODO: raise a nice exception
            assert False


class AbstractEmitter(Brick):

    @abstractmethod
    def emit(self, readouts):
        pass

    @abstractmethod
    def cost(self, readouts, outputs):
        pass

    @abstractmethod
    def initial_outputs(self, batch_size, *args, **kwargs):
        pass


class AbstractFeedback(Brick):

    @abstractmethod
    def feedback(self, outputs):
        pass


class AbstractReadout(AbstractEmitter, AbstractFeedback):
    """A base class for a readout component of a sequence generator.

    Yields outputs combining information from multiple sources.

    """
    @abstractmethod
    def readout(self, **kwargs):
        pass


class AbstractAttentionTransition(Brick):
    """A base class for a transition component of a sequence generator.

    A recurrent transition combined with an attention mechanism.

    """
    @abstractmethod
    def apply(self, **kwargs):
        pass

    @abstractmethod
    def take_look(self, **kwargs):
        pass

    @abstractmethod
    def initial_state(self, name, batch_size, *args, **kwargs):
        pass


class Readout(AbstractReadout):
    """Readout brick with separated emitting and feedback parts."""

    @lazy
    def __init__(self, readout_dim, emitter=None, feedbacker=None, **kwargs):
        super(Readout, self).__init__(**kwargs)

        if not emitter:
            emitter = TrivialEmitter(readout_dim)
        if not feedbacker:
            feedbacker = TrivialFeedback(readout_dim)
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        self.children = [self.emitter, self.feedbacker]

    def _push_allocation_config(self):
        self.emitter.readout_dim = self.get_dim('readouts')
        self.feedbacker.output_dim = self.get_dim('outputs')

    @application
    def emit(self, readouts):
        return self.emitter.emit(readouts)

    @application
    def cost(self, readouts, outputs):
        return self.emitter.cost(readouts, outputs)

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return self.emitter.initial_outputs(batch_size, **kwargs)

    @application(outputs=['feedback'])
    def feedback(self, outputs):
        return self.feedbacker.feedback(outputs)

    def get_dim(self, name):
        if name == 'outputs':
            return self.emitter.get_dim(name)
        elif name == 'feedback':
            return self.feedbacker.get_dim(name)
        elif name == 'readouts':
            return self.readout_dim
        return super(Readout, self).get_dim(name)


class LinearReadout(Readout):
    """Readout computed as sum of linear projections.

    Parameters
    ----------
    readout_dim : int
        The dimensionality of the readout.
    source_names : list of strs
        The names of information sources.

    """
    @lazy
    def __init__(self, readout_dim, source_names,
                 weights_init, biases_init, **kwargs):
        super(LinearReadout, self).__init__(readout_dim, **kwargs)
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        self.projectors = [MLP(name="project_{}".format(name),
                               activations=[Identity()])
                           for name in self.source_names]
        self.children.extend(self.projectors)

    def _push_allocation_config(self):
        super(LinearReadout, self)._push_allocation_config()
        for name, projector in zip(self.source_names, self.projectors):
            projector.dims[0] = self.source_dims[name]
            projector.dims[-1] = self.readout_dim

    def _push_initialization_config(self):
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @application
    def readout(self, **kwargs):
        projections = [projector.apply(kwargs[name]) for name, projector in
                       zip(self.source_names, self.projectors)]
        if len(projections) == 1:
            return projections[0]
        return sum(projections[1:], projections[0])


class TrivialEmitter(AbstractEmitter):

    @lazy
    def __init__(self, readout_dim, **kwargs):
        super(TrivialEmitter, self).__init__(**kwargs)
        self.readout_dim = readout_dim

    @application
    def emit(self, readouts):
        return readouts

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return tensor.zeros((batch_size, self.readout_dim))

    def get_dim(self, name):
        if name == 'outputs':
            return self.readout_dim
        return super(TrivialEmitter, self).get_dim(name)


class SoftmaxEmitter(AbstractEmitter, DefaultRNG):


    def _probs(self, readouts):
        shape = readouts.shape
        return tensor.nnet.softmax(readouts.reshape(
            (tensor.prod(shape[:-1]), shape[-1]))).reshape(shape)

    @application
    def emit(self, readouts):
        probs = self._probs(readouts)
        return self.theano_rng.multinomial(pvals=probs).argmax(axis=-1)

    @application
    def cost(self, readouts, outputs):
        probs = self._probs(readouts)
        max_output = probs.shape[-1]
        flat_outputs = outputs.flatten()
        num_outputs = flat_outputs.shape[0]
        return -tensor.log(
            probs.flatten()[max_output * tensor.arange(num_outputs)
                            + flat_outputs].reshape(outputs.shape))

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return tensor.zeros((batch_size,), dtype='int64')

    def get_dim(self, name):
        if name == 'outputs':
            return 0
        return super(SoftmaxEmitter, self).get_dim(name)


class TrivialFeedback(AbstractFeedback):

    @lazy
    def __init__(self, output_dim, **kwargs):
        super(TrivialFeedback, self).__init__(**kwargs)
        self.output_dim = output_dim

    @application(outputs=['feedback'])
    def feedback(self, outputs):
        return outputs

    def get_dim(self, name):
        if name == 'feedback':
            return self.output_dim
        return super(TrivialFeedback, self).get_dim(name)



class LookupFeedback(AbstractFeedback):

    @lazy
    def __init__(self, num_outputs, feedback_dim, **kwargs):
        super(LookupFeedback, self).__init__(**kwargs)
        self.num_outputs = num_outputs
        self.feedback_dim = feedback_dim

        self.lookup = LookupTable(num_outputs, feedback_dim,
                                  kwargs.get("weights_init"))
        self.children = [self.lookup]

    def _push_allocation_config(self):
        self.lookup.length = self.num_outputs
        self.lookup.dim = self.feedback_dim

    def _push_initialization_config(self):
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @application
    def feedback(self, outputs, **kwargs):
        assert self.output_dim == 0
        return self.lookup.lookup(outputs)

    def get_dim(self, name):
        if name == 'feedback':
            return self.feedback_dim
        return super(LookupFeedback, self).get_dim(name)


class FakeAttentionTransition(AbstractAttentionTransition):
    """Adds fake attention interface to a transition."""

    def __init__(self, transition, **kwargs):
        super(FakeAttentionTransition, self).__init__(**kwargs)
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        self.state_names = transition.apply.states
        self.context_names = transition.apply.contexts
        self.glimpse_names = []

        self.children = [self.transition]

    def _push_initialization_config(self):
        # TODO: stop copy-pasting this code
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @recurrent
    def apply(self, *args, **kwargs):
        return self.transition.apply(*args, **dict_union(kwargs, iterate=False))

    @apply.delegate
    def apply_delegate(self):
        return self.transition.apply

    @application
    def initial_state(self, state_name, batch_size, *args, **kwargs):
        return self.transition.initial_state(state_name, batch_size,
                                             *args, **kwargs)

    @application(outputs=[])
    def take_look(self, *args, **kwargs):
        return None

    def get_dim(self, name):
        return self.transition.get_dim(name)

class Fork(Brick):

    @lazy
    def __init__(self, fork_names, prototype=None, **kwargs):
        super(Fork, self).__init__(**kwargs)

        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        if not self.prototype:
            self.prototype = MLP([Identity()])
        self.forkers = []
        for name in self.fork_names:
            self.forkers.append(copy.deepcopy(self.prototype))
            self.forkers[-1].name = "fork_" + name
        self.children = self.forkers

    def _push_allocation_config(self):
        for name, forker in zip(self.fork_names, self.forkers):
            forker.dims[0] = self.input_dim
            forker.dims[-1] = self.fork_dims[name]

    def _push_initialization_config(self):
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @application
    def apply(self, inp):
        return [forker.apply(inp) for forker in self.forkers]

    @apply.property('outputs')
    def apply_outputs(self):
        return self.fork_names


class SequenceGenerator(BaseSequenceGenerator):
    """A more user-friendly interface for BaseSequenceGenerator."""

    def __init__(self, readout, transition, attention=None,
                 fork_inputs=None,
                 weights_init=None, biases_init=None, **kwargs):
        if attention:
            raise NotImplementedError()
        if not fork_inputs:
            fork_inputs = [name for name in transition.apply.sequences
                           if name != 'mask']

        fork = Fork(fork_inputs)
        transition = FakeAttentionTransition(transition,
                                             name="with_fake_attention")
        super(SequenceGenerator, self).__init__(
            readout, fork, transition, weights_init, biases_init, **kwargs)
