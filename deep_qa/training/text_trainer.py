from copy import deepcopy
from typing import Any, Dict, List, Tuple
import logging
import dill as pickle

from keras import backend as K
from keras.layers import Dense, Dropout, Layer, TimeDistributed
from overrides import overrides
import numpy

from ..common.checks import ConfigurationError
from ..common.params import get_choice_with_default
from ..data.dataset import TextDataset
from ..data.instances.instance import Instance, TextInstance
from ..data.embeddings import PretrainedEmbeddings
from ..data.tokenizers import tokenizers
from ..data.data_indexer import DataIndexer
from ..layers.encoders import encoders, set_regularization_params, seq2seq_encoders
from ..layers.time_distributed_embedding import TimeDistributedEmbedding
from .trainer import Trainer

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class TextTrainer(Trainer):
    # pylint: disable=line-too-long
    """
    This is a Trainer that deals with word sequences as its fundamental data type (any TextDataset
    or TextInstance subtype is fine).  That means we have to deal with padding, with converting
    words (or characters) to indices, and encoding word sequences.  This class adds methods on top
    of Trainer to deal with all of that stuff.

    This class has five kinds of methods:

    (1) protected methods that are overriden from :class:`~deep_qa.training.trainer.Trainer`, and
        which you shouldn't need to worry about
    (2) utility methods for building models, intended for use by subclasses
    (3) abstract methods that determine a few key points of behavior in concrete subclasses (e.g.,
        what your input data type is)
    (4) model-specific methods that you `might` have to override, depending on what your model
        looks like - similar to (3), but simple models don't need to override these
    (5) private methods that you shouldn't need to worry about

    There are two main ways you're intended to interact with this class, then: by calling the
    utility methods when building your model, and by customizing the behavior of your concrete
    model by using the parameters to this class.

    Parameters
    ----------
    pretrained_embeddings_file: string, optional
        If specified, we will use the vectors in this file as our embedding layer.  You can
        optionally keep these fixed or fine tune them, or learn a projection on top of them.
    fine_tune_embeddings: bool, optional (default=False)
        If we're using pre-trained embeddings, should we fine tune them?
    project_embeddings: bool, optional (default=False)
        Should we have a projection layer on top of our embedding layer? (mostly useful with
        pre-trained embeddings)
    embedding_dim: Dict[str, int], optional (default={'words': 50, 'characters': 8})
        Number of dimensions to use for embeddings.  This is a dictionary, keyed by vocabulary
        name.  The two default vocabulary names that are used are "words" and "characters".  The
        'words' embedding_dim is also used by default for setting hidden layer sizes in things like
        LSTMs, if you don't specify an output size in the ``encoder`` params.
    embedding_dropout: float, optional (default=0.5)
        Dropout parameter to apply to the embedding layer
    num_sentence_words: int, optional (default=None)
        Upper limit on length of word sequences in the training data. Ignored during testing (we
        use the value set at training time, either from this parameter or from a loaded model).  If
        this is not set, we'll calculate a max length from the data.
    num_word_characters: int, optional (default=None)
        Upper limit on length of words in the training data. Only applicable for "words and
        characters" text encoding.
    tokenizer: Dict[str, Any], optional (default={})
        Which tokenizer to use for ``TextInstances``.  See
        :mod:``deep_qa.data.tokenizers.tokenizer`` for more information.
    encoder: Dict[str, Dict[str, Any]], optional (default={'default': {}})
        These parameters specify the kind of encoder used to encode any word sequence input.  An
        encoder takes a sequence of vectors and returns a single vector.

        If given, this must be a dict, where each key is a name that can be used for encoders in
        the model, and the value corresponding to the key is a set of parameters that will be
        passed on to the constructor of the encoder.  We will use the "type" key in this dict
        (which must match one of the keys in `encoders`) to determine the type of the encoder, then
        pass the remaining args to the encoder constructor.

        Hint: Use ``"lstm"`` or ``"cnn"`` for sentences, ``"treelstm"`` for logical forms, and
        ``"bow"`` for either.
    encoder_fallback_behavior: string, optional (default="crash")
        Determines the behavior when an encoder is asked for by name, but you have not given
        parameters for an encoder with that name.  See ``_get_encoder`` for more information.
    seq2seq_encoder: Dict[str, Dict[str, Any]], optional (default={'default': {'encoder_params': {}, 'wrapper_params: {}}})
        Like ``encoder``, except seq2seq encoders return a sequence of vectors instead of a single
        vector (the difference between our "encoders" and "seq2seq encoders" is the difference in
        Keras between ``LSTM()`` and ``LSTM(return_sequences=True)``).
    seq2seq_encoder_fallback_behavior: string, optional (default="crash")
        Determines the behavior when a seq2seq encoder is asked for by name, but you have not given
        parameters for an encoder with that name.  See ``_get_seq2seq_encoder`` for more
        information.
    """
    # pylint: enable=line-too-long
    def __init__(self, params: Dict[str, Any]):
        self.pretrained_embeddings_file = params.pop('pretrained_embeddings_file', None)
        self.fine_tune_embeddings = params.pop('fine_tune_embeddings', False)
        self.project_embeddings = params.pop('project_embeddings', False)
        self.embedding_dim = params.pop('embedding_dim', {'words': 50, 'characters': 8})
        self.embedding_dropout = params.pop('embedding_dropout', 0.5)
        self.num_sentence_words = params.pop('num_sentence_words', None)
        self.num_word_characters = params.pop('num_word_characters', None)

        tokenizer_params = params.pop('tokenizer', {})
        tokenizer_choice = get_choice_with_default(tokenizer_params, 'type', list(tokenizers.keys()))
        self.tokenizer = tokenizers[tokenizer_choice](tokenizer_params)
        # Note that the way this works is a little odd - we need each Instance object to do the
        # right thing when we call instance.words() and instance.to_indexed_instance().  So we set
        # a class variable on TextInstance so that _all_ TextInstance objects use the setting that
        # we read here.
        TextInstance.tokenizer = self.tokenizer

        self.encoder_params = params.pop('encoder', {'default': {}})
        self.encoder_fallback_behavior = params.pop('encoder_fallback_behavior', 'crash')
        self.seq2seq_encoder_params = params.pop('seq2seq_encoder', {'default': {"encoder_params": {},
                                                                                 "wrapper_params": {}}})
        self.seq2seq_encoder_fallback_behavior = params.pop('seq2seq_encoder_fallback_behavior', 'crash')
        super(TextTrainer, self).__init__(params)

        self.name = "TextTrainer"
        self.data_indexer = DataIndexer()

        # Model-specific member variables that will get set and used later.  For many of these, we
        # don't want to set them now, because they use max length information that only gets set
        # after reading the training data.
        self.embedding_layers = {}
        self.encoder_layers = {}
        self.seq2seq_encoder_layers = {}
        self._sentence_encoder_model = None

    ###########################
    # Overriden Trainer methods - you shouldn't have to worry about these, though for some
    # advanced uses you might override some of them, especially _get_custom_objects.
    ###########################

    @overrides
    def _prepare_data(self, dataset: TextDataset, for_train: bool,
                      update_data_indexer=True):
        """
        Takes dataset, which could be a complex tuple for some classes, and produces as output a
        tuple of (inputs, labels), which can be used directly with Keras to either train or
        evaluate self.model.
        """
        if for_train and update_data_indexer:
            logger.info("Fitting data indexer word dictionary.")
            self.data_indexer.fit_word_dictionary(dataset)
        logger.info("Indexing dataset")
        indexed_dataset = dataset.to_indexed_dataset(self.data_indexer)
        max_lengths = self._get_max_lengths()
        logger.info("Padding dataset to lengths %s", str(max_lengths))
        indexed_dataset.pad_instances(max_lengths)
        if for_train:
            self._set_max_lengths(indexed_dataset.max_lengths())
        inputs, labels = indexed_dataset.as_training_data()
        if isinstance(inputs[0], tuple):
            inputs = [numpy.asarray(x) for x in zip(*inputs)]
        else:
            inputs = numpy.asarray(inputs)
        if isinstance(labels[0], tuple):
            labels = [numpy.asarray(x) for x in zip(*labels)]
        else:
            labels = numpy.asarray(labels)
        return inputs, labels

    @overrides
    def _prepare_instance(self, instance: TextInstance, make_batch: bool=True):
        indexed_instance = instance.to_indexed_instance(self.data_indexer)
        indexed_instance.pad(self._get_max_lengths())
        inputs, label = indexed_instance.as_training_data()
        if make_batch:
            if isinstance(inputs, tuple):
                inputs = [numpy.expand_dims(x, axis=0) for x in inputs]
            else:
                inputs = numpy.expand_dims(inputs, axis=0)
        return inputs, label

    @overrides
    def _set_params_from_model(self):
        self._set_max_lengths_from_model()

    @overrides
    def _save_auxiliary_files(self):
        super(TextTrainer, self)._save_auxiliary_files()
        data_indexer_file = open("%s_data_indexer.pkl" % self.model_prefix, "wb")
        pickle.dump(self.data_indexer, data_indexer_file)
        data_indexer_file.close()

    @overrides
    def _load_auxiliary_files(self):
        super(TextTrainer, self)._load_auxiliary_files()
        data_indexer_file = open("%s_data_indexer.pkl" % self.model_prefix, "rb")
        self.data_indexer = pickle.load(data_indexer_file)
        data_indexer_file.close()

    @overrides
    def _load_dataset_from_files(self, files: List[str]):
        """
        This method assumes you have a TextDataset that can be read from a single file.  If you
        have something more complicated, you'll need to override this method (though, a solver that
        has background information could call this method, then do additional processing on the
        rest of the list, for instance).
        """
        return TextDataset.read_from_file(files[0], self._instance_type())

    @overrides
    def _overall_debug_output(self, output_dict: Dict[str, numpy.array]) -> str:
        """
        We'll do something different here: if "embedding" is in output_dict, we'll output the
        embedding matrix at the top of the debug file.  Note that this could be _huge_ - you should
        only do this for debugging on very simple datasets.
        """
        result = super(TextTrainer, self)._overall_debug_output(output_dict)
        if any('embedding' in layer_name for layer_name in output_dict.keys()):
            embedding_layers = set([n for n in output_dict.keys() if 'embedding' in n])
            for embedding_layer in embedding_layers:
                if '_projection' in embedding_layer:
                    continue
                if embedding_layer.startswith('combined_'):
                    continue
                result += self.__render_embedding_matrix(embedding_layer)
        return result

    @classmethod
    def _get_custom_objects(cls):
        custom_objects = super(TextTrainer, cls)._get_custom_objects()
        custom_objects["TimeDistributedEmbedding"] = TimeDistributedEmbedding
        for value in encoders.values():
            if value.__name__ not in ['LSTM']:
                custom_objects[value.__name__] = value
        for name, layer in TextInstance.tokenizer.get_custom_objects().items():
            custom_objects[name] = layer
        return custom_objects

    #################
    # Utility methods - meant to be called by subclasses, not overriden
    #################

    def _get_sentence_shape(self, sentence_length: int=None) -> Tuple[int]:
        """
        Returns a tuple specifying the shape of a tensor representing a sentence.  This is not
        necessarily just (self.num_sentence_words,), because different text_encodings lead to
        different tensor shapes.  If you have an input that is a sequence of words, you need to
        call this to get the shape to pass to an ``Input`` layer.  If you don't, your model won't
        work correctly for all tokenizers.
        """
        if sentence_length is None:
            # This can't be the default value for the function argument, because
            # self.num_sentence_words will not have been set at class creation time.
            sentence_length = self.num_sentence_words
        return self.tokenizer.get_sentence_shape(sentence_length, self.num_word_characters)

    def _embed_input(self, input_layer: Layer, embedding_name: str="embedding"):
        """
        This function embeds a word sequence input, using an embedding defined by
        ``embedding_name``.  You should call this function in your ``_build_model`` method any time
        you want to convert word indices into word embeddings.  Note that if this is used in
        conjunction with ``_get_sentence_shape``, we will do the correct thing for whatever
        :class:`~deep_qa.data.tokenizers.tokenizer.Tokenizer` you use.  The actual input to this
        might be words and characters, and we might actually do a concatenation of a word embedding
        and a character-level encoder.  All of this is handled transparently to your concrete model
        subclass, if you use the API correctly, calling ``_get_sentence_shape()`` to get the shape
        for your ``Input`` layer, and passing that input layer into this ``_embed_input()`` method.

        We need to take the input Layer here, instead of just returning a Layer that you can use as
        you wish, because we might have to apply several layers to the input, depending on the
        parameters you specified for embedding things.  So we return, essentially,
        `embedding(input_layer)`.

        The input layer can have arbitrary shape, as long as it ends with a word sequence.  For
        example, you could pass in a single sentence, a set of sentences, or a set of sets of
        sentences, and we will handle them correctly.

        Internally, we will create a dictionary mapping embedding names to embedding layers, so if
        you have several things you want to embed with the same embedding layer, be sure you use
        the same name each time (or just don't pass a name, which accomplishes the same thing).  If
        for some reason you want to have different embeddings for different inputs, use a different
        name for the embedding.

        In this function, we pass the work off to self.tokenizer, which might need to do some
        additional processing to actually give you a word embedding (e.g., if your text encoder
        uses both words and characters, we need to run the character encoder and concatenate the
        result with a word embedding).
        """
        return self.tokenizer.embed_input(input_layer, self.__get_embedded_input, self, embedding_name)

    def _get_encoder(self, name="default", fallback_behavior: str=None):
        """
        This method is intended to be used in your ``_build_model`` implementation, any time you
        want to convert a sequence of vectors into a single vector.  The encoder ``name``
        corresponds to entries in the ``encoder`` parameter passed to the constructor of this
        object, allowing you to customize the kind and behavior of the encoder just through
        parameters.

        A sentence encoder takes as input a sequence of word embeddings, and returns as output a
        single vector encoding the sentence.  This is typically either a simple RNN or an LSTM, but
        could be more complex, if the "sentence" is actually a logical form.

        Parameters
        ----------
        name : str, optional (default="default")
            The name of the encoder.  Multiple calls to ``_get_encoder`` using the same name will
            return the same encoder.  To get parameters for creating the encoder, we look in
            ``self.encoder_params``, which is specified by the ``encoder`` parameter in
            ``self.__init__``.  If ``name`` is not a key in ``self.encoder_params``, the behavior
            is defined by the ``fallback_behavior`` parameter.
        fallback_behavior : str, optional (default=None)
            Determines what to do when ``name`` is not a key in ``self.encoder_params``.  If you
            pass ``None`` (the default), we will use ``self.encoder_fallback_behavior``, specified
            by the ``encoder_fallback_behavior`` parameter to ``self.__init__``.  There are three
            options:

            - ``"crash"``: raise an error.  This is the default for
              ``self.encoder_fallback_behavior``.  The intention is to help you find bugs - if you
              specify a particular encoder name in ``self._build_model`` without giving a fallback
              behavior, you probably wanted to use a particular set of parameters, so we crash if
              they are not provided.
            - ``"use default params"``: In this case, we return a new encoder created with
              ``self.encoder_params["default"]``.
            - ``"use default encoder"``: In this case, we `reuse` the encoder created with
              ``self.encoder_params["default"]``.  This effectively changes the ``name`` parameter
              to ``"default"`` when the given ``name`` is not in ``self.encoder_params``.
        """
        if fallback_behavior is None:
            fallback_behavior = self.encoder_fallback_behavior
        if name in self.encoder_layers:
            # If we've already created this encoder, we can just return it.
            return self.encoder_layers[name]
        if name not in self.encoder_params and name != "default":
            # If we haven't, we need to check that we _can_ create it, and decide _how_ to create
            # it.
            if fallback_behavior == "crash":
                raise ConfigurationError("You asked for a named encoder (" + name + "), but "
                                         "did not provide parameters for that encoder")
            elif fallback_behavior == "use default encoder":
                name = "default"
                params = deepcopy(self.encoder_params.get(name, {}))
            elif fallback_behavior == "use default params":
                params = deepcopy(self.encoder_params["default"])
            else:
                raise ConfigurationError("Unrecognized fallback behavior: " + fallback_behavior)
        else:
            params = deepcopy(self.encoder_params.get(name, {}))
        if name not in self.encoder_layers:
            # We need to check if we've already created this again, because in some cases we change
            # the name in the logic above.
            encoder_layer_name = name + "_encoder"
            new_encoder = self.__get_new_encoder(params, encoder_layer_name)
            self.encoder_layers[name] = new_encoder
        return self.encoder_layers[name]

    def _get_seq2seq_encoder(self, name="default", fallback_behavior: str=None):
        """
        This method is intended to be used in your ``_build_model`` implementation, any time you
        want to convert a sequence of vectors into another sequence of vector.  The encoder
        ``name`` corresponds to entries in the ``encoder`` parameter passed to the constructor of
        this object, allowing you to customize the kind and behavior of the encoder just through
        parameters.

        A seq2seq encoder takes as input a sequence of vectors, and returns as output a sequence of
        vectors.  This method is essentially identical to ``_get_encoder``, except that it gives an
        encoder that returns a sequence of vectors instead of a single vector.

        Parameters
        ----------
        name : str, optional (default="default")
            The name of the encoder.  Multiple calls to ``_get_seq2seq_encoder`` using the same
            name will return the same encoder.  To get parameters for creating the encoder, we look
            in ``self.seq2seq_encoder_params``, which is specified by the ``seq2seq_encoder``
            parameter in ``self.__init__``.  If ``name`` is not a key in
            ``self.seq2seq_encoder_params``, the behavior is defined by the ``fallback_behavior``
            parameter.
        fallback_behavior : str, optional (default=None)
            Determines what to do when ``name`` is not a key in ``self.seq2seq_encoder_params``.
            If you pass ``None`` (the default), we will use
            ``self.seq2seq_encoder_fallback_behavior``, specified by the
            ``seq2seq_encoder_fallback_behavior`` parameter to ``self.__init__``.  There are three
            options:

            - ``"crash"``: raise an error.  This is the default for
              ``self.seq2seq_encoder_fallback_behavior``.  The intention is to help you find bugs -
              if you specify a particular encoder name in ``self._build_model`` without giving a
              fallback behavior, you probably wanted to use a particular set of parameters, so we
              crash if they are not provided.
            - ``"use default params"``: In this case, we return a new encoder created with
              ``self.seq2seq_encoder_params["default"]``.
            - ``"use default encoder"``: In this case, we `reuse` the encoder created with
              ``self.seq2seq_encoder_params["default"]``.  This effectively changes the ``name``
              parameter to ``"default"`` when the given ``name`` is not in
              ``self.seq2seq_encoder_params``.
        """
        if fallback_behavior is None:
            fallback_behavior = self.seq2seq_encoder_fallback_behavior
        if name in self.seq2seq_encoder_layers:
            # If we've already created this encoder, we can just return it.
            return self.seq2seq_encoder_layers[name]
        if name not in self.seq2seq_encoder_params:
            # If we haven't, we need to check that we _can_ create it, and decide _how_ to create
            # it.
            if fallback_behavior == "crash":
                raise ConfigurationError("You asked for a named seq2seq encoder (" + name + "), "
                                         "but did not provide parameters for that encoder")
            elif fallback_behavior == "use default encoder":
                name = "default"
                params = deepcopy(self.seq2seq_encoder_params[name])
            elif fallback_behavior == "use default params":
                params = deepcopy(self.seq2seq_encoder_params["default"])
            else:
                raise ConfigurationError("Unrecognized fallback behavior: " + fallback_behavior)
        else:
            params = deepcopy(self.seq2seq_encoder_params[name])
        if name not in self.seq2seq_encoder_layers:
            # We need to check if we've already created this again, because in some cases we change
            # the name in the logic above.
            encoder_layer_name = name + "_encoder"
            new_encoder = self.__get_new_seq2seq_encoder(params, encoder_layer_name)
            self.seq2seq_encoder_layers[name] = new_encoder
        return self.seq2seq_encoder_layers[name]

    def _set_text_lengths_from_model_input(self, input_slice):
        """
        Given an input slice (a tuple) from a model representing the max length of the sentences
        and the max length of each words, set the padding max lengths.  This gets called when
        loading a model, and is necessary to get padding correct when using loaded models.
        Subclasses need to call this in their ``_set_max_lengths_from_model`` method.

        Parameters
        ----------
        input_slice : tuple
            A slice from a concrete model class that represents an input word sequence. The tuple
            must be of length one or two, and the first dimension should correspond to the length
            of the sentences while the second dimension (if provided) should correspond to the
            max length of the words in each sentence.
        """
        if len(input_slice) > 2:
            raise ValueError("Length of input tuple must be "
                             "2 or 1, got input tuple of "
                             "length {}".format(len(input_slice)))
        self.num_sentence_words = input_slice[0]
        if len(input_slice) == 2:
            self.num_word_characters = input_slice[1]

    ##################
    # Abstract methods - you MUST override these
    ##################

    def _instance_type(self) -> Instance:
        """
        When reading datasets, what :class:`~deep_qa.data.instances.instance.Instance` type should
        we create?  The ``Instance`` class contains code that creates actual numpy arrays, so this
        instance type determines the inputs that you will get to your model, and the outputs that
        are used for training.
        """
        raise NotImplementedError

    def _set_max_lengths_from_model(self):
        """
        This gets called when loading a saved model.  It is analogous to ``_set_max_lengths``, but
        needs to set all of the values set in that method just by inspecting the loaded model.  If
        we didn't have this, we would not be able to correctly pad data after loading a model.
        """
        raise NotImplementedError

    ########################
    # Model-specific methods - if you do anything complicated, you probably need to override these,
    # but simple models might be able to get by with just the default implementation
    ########################

    def _get_max_lengths(self) -> Dict[str, int]:
        """
        This is about padding.  Any solver will have some number of things that need padding in
        order to make a compilable model, like the length of a sentence.  This method returns a
        dictionary of all of those things, mapping a length key to an int.

        Here we return the lengths that are applicable to encoding words and sentences.  If you
        have additional padding dimensions, call super()._get_max_lengths() and then update the
        dictionary.
        """
        return self.tokenizer.get_max_lengths(self.num_sentence_words, self.num_word_characters)

    def _set_max_lengths(self, max_lengths: Dict[str, int]):
        """
        This is about padding.  Any solver will have some number of things that need padding in
        order to make a compilable model, like the length of a sentence.  This method sets those
        variables given a dictionary of lengths, perhaps computed from training data or loaded from
        a saved model.
        """
        self.num_sentence_words = max_lengths['num_sentence_words']
        self.num_word_characters = max_lengths.get('num_word_characters', None)

    #################
    # Private methods - you can't to override these.  If you find yourself needing to, we can
    # consider making them protected instead.
    #################

    def __get_embedded_input(self,
                             input_layer: Layer,
                             embedding_name: str="embedding",
                             vocab_name: str='words'):
        """
        This function does most of the work for self._embed_input.  We pass this method to the
        tokenizer, so it can get whatever embedding layers it needs.

        We allow for multiple vocabularies, e.g., if you want to embed both characters and words
        with separate embedding matrices.
        """
        embedding_dim = self.embedding_dim[vocab_name]
        if embedding_name not in self.embedding_layers:
            self.embedding_layers[embedding_name] = self.__get_new_embedding(embedding_name,
                                                                             embedding_dim,
                                                                             vocab_name)

        embedding_layer, projection_layer = self.embedding_layers[embedding_name]
        embedded_input = embedding_layer(input_layer)
        if projection_layer is not None:
            for _ in range(2, K.ndim(input_layer)):  # 2 here to account for batch_size.
                projection_layer = TimeDistributed(projection_layer, name="timedist_" + projection_layer.name)
            embedded_input = projection_layer(embedded_input)
        if self.embedding_dropout > 0.0:
            embedded_input = Dropout(self.embedding_dropout)(embedded_input)

        return embedded_input

    def __get_new_embedding(self, name: str, embedding_dim: int, vocab_name: str='words'):
        """
        Creates an Embedding Layer (and possibly also a Dense projection Layer) based on the
        parameters you've passed to the TextTrainer.  These could be pre-trained embeddings or not,
        could include a projection or not, and so on.
        """
        if vocab_name == 'words' and self.pretrained_embeddings_file:
            embedding_layer = PretrainedEmbeddings.get_embedding_layer(
                    self.pretrained_embeddings_file,
                    self.data_indexer,
                    self.fine_tune_embeddings,
                    name=name)
        else:
            # TimeDistributedEmbedding works with inputs of any shape.
            embedding_layer = TimeDistributedEmbedding(
                    input_dim=self.data_indexer.get_vocab_size(vocab_name),
                    output_dim=embedding_dim,
                    mask_zero=True,  # this handles padding correctly
                    name=name)
        projection_layer = None
        if self.project_embeddings:
            projection_layer = TimeDistributed(Dense(units=embedding_dim,),
                                               name=name + '_projection')
        return embedding_layer, projection_layer

    def __get_new_encoder(self, params: Dict[str, Any], name: str):
        encoder_type = get_choice_with_default(params, "type", list(encoders.keys()))
        params["name"] = name
        params.setdefault("units", self.embedding_dim['words'])
        set_regularization_params(encoder_type, params)
        return encoders[encoder_type](**params)

    def __get_new_seq2seq_encoder(self, params: Dict[str, Any], name="seq2seq_encoder"):
        encoder_params = params["encoder_params"]
        wrapper_params = params["wrapper_params"]
        wrapper_params["name"] = name
        seq2seq_encoder_type = get_choice_with_default(encoder_params,
                                                       "type",
                                                       list(seq2seq_encoders.keys()))
        encoder_params.setdefault("units", self.embedding_dim['words'])
        set_regularization_params(seq2seq_encoder_type, encoder_params)
        return seq2seq_encoders[seq2seq_encoder_type](**params)

    def __render_embedding_matrix(self, embedding_name: str) -> str:
        result = 'Embedding matrix for %s:\n' % embedding_name
        embedding_weights = self.embedding_layers[embedding_name][0].get_weights()[0]
        for i in range(self.data_indexer.get_vocab_size()):
            word = self.data_indexer.get_word_from_index(i)
            word_vector = '[' + ' '.join('%.4f' % x for x in embedding_weights[i]) + ']'
            result += '%s\t%s\n' % (word, word_vector)
        result += '\n'
        return result
