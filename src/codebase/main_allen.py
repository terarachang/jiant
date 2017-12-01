import os
import pdb
import sys
import time
import argparse
import logging as log
from collections import defaultdict
import _pickle as pkl
import numpy as np

import torch

#from allennlp.commands.evaluate import evaluate
from allennlp.common.params import Params
from allennlp.data import Instance, Dataset, Vocabulary
from allennlp.data.fields import TextField, LabelField, NumericField
from allennlp.data.iterators import BasicIterator
from allennlp.data.token_indexers import SingleIdTokenIndexer, TokenCharactersIndexer
from allennlp.modules.text_field_embedders import BasicTextFieldEmbedder
from allennlp.modules.token_embedders import Embedding, TokenCharactersEncoder
from allennlp.modules.similarity_functions import LinearSimilarity
from allennlp.modules.seq2vec_encoders import BagOfEmbeddingsEncoder, \
                                              CnnEncoder
from allennlp.modules.seq2seq_encoders import Seq2SeqEncoder as s2s_e

PATH_TO_PKG = '../'
sys.path.append(os.path.join(os.path.dirname(__file__), PATH_TO_PKG))
from codebase.tasks import MSRPTask, MultiNLITask, QuoraTask, \
        RTETask, RTE8Task, SNLITask, SSTTask, STS14Task
from codebase.models_allen import HeadlessBiDAF, HeadlessSentEncoder, \
                                    MultiTaskModel, HeadlessPairEncoder
from codebase.trainer import MultiTaskTrainer
from codebase.evaluate import evaluate
from codebase.utils.encoders import MultiLayerRNNEncoder
from codebase.utils.seq_batch import SequenceBatch
from codebase.utils.token_embedder import TokenEmbedder
from codebase.utils.utils import GPUVariable

PATH_PREFIX = '/misc/vlgscratch4/BowmanGroup/awang/processed_data/' + \
              'mtl-sentence-representations/'

NAME2TASK = {'msrp': MSRPTask, 'mnli': MultiNLITask,
             'quora': QuoraTask, 'rte8': RTE8Task,
             'rte': RTETask, 'snli': SNLITask,
             'sst': SSTTask, 'sts14': STS14Task,
             'small':QuoraTask, 'small2': QuoraTask}
NAME2DATA = {'msrp': PATH_PREFIX + 'MRPC/',
             'mnli': PATH_PREFIX + 'MNLI/',
             'quora': PATH_PREFIX + 'Quora/quora_duplicate_questions.tsv',
             'rte': PATH_PREFIX + 'rte/',
             'rte8': PATH_PREFIX + 'rte8/semeval2013-Task7-2and3way',
             'snli': PATH_PREFIX + 'SNLI/',
             'sst': PATH_PREFIX + 'SST/binary/',
             'sts14': PATH_PREFIX + 'STS/STS14-en-test',
             'small': PATH_PREFIX + 'Quora/quora_small.tsv',
             'small2': PATH_PREFIX + 'Quora/quora_small.tsv'}

# lazy way to map tasks to preprocessed tasks
NAME2SAVE = {'msrp': PATH_PREFIX + 'MRPC/msrp_task.pkl',
             'mnli': PATH_PREFIX + 'MNLI/mnli_task.pkl',
             'quora': PATH_PREFIX + 'Quora/quora_task.pkl',
             'rte': PATH_PREFIX + 'rte/rte_task.pkl',
             'rte8': PATH_PREFIX + 'rte8/rte8_task.pkl',
             'snli': PATH_PREFIX + 'SNLI/snli_task.pkl',
             'sst': PATH_PREFIX + 'SST/sst_task.pkl',
             'sts14': PATH_PREFIX + 'STS/sts14_task.pkl',
             'small': PATH_PREFIX + 'Quora/small_task.pkl',
             'small2': PATH_PREFIX + 'Quora/small2_task.pkl'}


def load_tasks(task_names, max_seq_len, load):
    '''
    Load tasks
    '''
    tasks = []
    for name in task_names:
        assert name in NAME2TASK, 'Task not found!'
        if os.path.isfile(NAME2SAVE[name]) and load:
            task = pkl.load(open(NAME2SAVE[name], 'rb'))
            log.info('\tLoaded existing task %s', name)
        else:
            task = NAME2TASK[name](NAME2DATA[name], max_seq_len, name)
            pkl.dump(task, open(NAME2SAVE[name], 'wb'))
        tasks.append(task)
    log.info("\tFinished loading tasks: %s.", ' '.join(
        [task.name for task in tasks]))
    return tasks

def build_classifiers(tasks, model, classifier_type, pair_enc, input_dim,
                      hid_dim, dropout):
    '''
    Build the classifier for each task
    '''
    for task in tasks:
        if task.pair_input:
            if pair_enc == 'bidaf':
                task_dim = input_dim * 10
            else:
                assert pair_enc == 'simple'
                task_dim = input_dim * 8
        else:
            task_dim = input_dim * 2
        model.build_classifier(task, classifier_type, task_dim, hid_dim,
                               dropout)
    return

def get_words(tasks):
    '''
    Get all words for all tasks for all splits for all sentences
    Return dictionary mapping words to frequencies.
    '''
    word2freq = defaultdict(int)
    for task in tasks:
        for split in [task.train_data_text, task.val_data_text,
                      task.test_data_text]:
            for sentence in split[0]:
                for word in sentence:
                    word2freq[word] += 1
            if task.pair_input:
                for sentence in split[1]:
                    for word in sentence:
                        word2freq[word] += 1
    return word2freq

def get_word_vecs(path, vocab, word_dim):
    word_vecs = {}
    with open(path) as fh:
        for line in fh:
            word, vec = line.split(' ', 1)
            if word in vocab:
                word_vecs[word] = np.array(list(map(float, vec.split())))
                assert len(word_vecs[word]) == word_dim, \
                        'Mismatch in word vector dimension'
    log.info('Found %d/%d words with word vectors', len(word_vecs),
             len(vocab))
    return word_vecs

def prepare_tasks(task_names, word_vecs_path, word_dim,
                  max_vocab_size, max_seq_len, vocab_path, exp_dir,
                  load_task, load_vocab, load_index):
    '''
    Prepare the tasks by:
        - Creating or loading the tasks
        - Building the vocabulary of words with word vectors
        - Index the tasks

    Args:
        - task_names (list[str]): list of task names
        - max_vocab_size (int): -1 for no max_vocab size

    Returns:
        - tasks (list[Task]): list of tasks
    '''

    tasks = load_tasks(task_names, max_seq_len, load_task)

    # get all words across all tasks, all splits, all sentences
    word2freq = get_words(tasks)

    # load word vectors for the words and build vocabulary
    word2vec = get_word_vecs(word_vecs_path, word2freq, word_dim)
    if load_vocab and os.path.exists(vocab_path + 'non_padded_namespaces.txt'):
        vocab = Vocabulary.from_files(vocab_path)
        # want to assert that all words have word vectors
        log.info('\tLoaded existing vocabulary from %s', vocab_path)
    else:
        if max_vocab_size < 0:
            max_vocab_size = None
        vocab = Vocabulary(counter=None, max_vocab_size=max_vocab_size)
        words_by_freq = [(word, freq) for word, freq in word2freq.items() if
                         word in word2vec]
        words_by_freq.sort(key=lambda x: x[1], reverse=True)
        for word, _ in words_by_freq:
            _ = vocab.add_token_to_namespace(word)
        if vocab_path:
            vocab.save_to_files(vocab_path)
            log.info('\tSaved vocabulary to %s', vocab_path)
    vocab_size = vocab.get_vocab_size('tokens')
    log.info("\tFinished building vocab. Using %d words", vocab_size)

    embeddings = np.zeros((vocab_size, word_dim))
    for idx in range(vocab_size): # kind of hacky
        word = vocab.get_token_from_index(idx)
        if word == '@@PADDING@@' or word == '@@UNKNOWN@@':
            continue
        try:
            assert word in word2vec
        except AssertionError as error:
            log.debug(error)
            pdb.set_trace()
        embeddings[idx] = word2vec[word]
    embeddings[vocab.get_token_index('@@PADDING@@')] = 0.
    embeddings = torch.FloatTensor(embeddings)

    # convert text data to AllenNLP text fields
    token_indexer = {"words": SingleIdTokenIndexer(),
                     "chars": TokenCharactersIndexer()}

    # index words and chars using vocab
    for task in tasks:
        template = exp_dir + '/%s_indexed_data.pkl'
        #task.train_data.index_instances(vocab)
        #task.val_data.index_instances(vocab)
        #task.test_data.index_instances(vocab)
        if not load_index or not os.path.exists(template % task.name):
            train, val, test = process_task(task, token_indexer, vocab)
            pkl.dump((train, val, test), open(template % task.name, 'wb'))
        else:
            train, val, test = pkl.load(open(template % task.name, 'rb'))
            log.info("\tReusing old indexing for %s.", task.name)
        task.train_data = train
        task.val_data = val
        task.test_data = test
    log.info("\tFinished indexing.")

    return tasks, vocab, embeddings

def process_task(task, token_indexer, vocab):
    '''
    Convert a task's splits into AllenNLP fields then
    Index the splits using the given vocab (experiment dependent)
    '''
    train_data = process_split(task.train_data_text, token_indexer,
                               task.pair_input, task.categorical)
    train_data.index_instances(vocab)
    val_data = process_split(task.val_data_text, token_indexer,
                             task.pair_input, task.categorical)
    val_data.index_instances(vocab)
    test_data = process_split(task.test_data_text, token_indexer,
                              task.pair_input, task.categorical)
    test_data.index_instances(vocab)
    return (train_data, val_data, test_data)

def process_split(split, token_indexer, pair_input, categorical):
    '''
    Convert a dataset of sentences into padded sequences of indices.

    Args:
        - split (list[list[str]]): list of inputs (possibly pair) and outputs
        - pair_input (int)
        - tok2idx (dict)

    Returns:
    '''
    if pair_input:
        inputs1 = [TextField(sent, token_indexers=token_indexer) for \
                   sent in split[0]]
        inputs2 = [TextField(sent, token_indexers=token_indexer) for \
                   sent in split[1]]
        if categorical:
            labels = [LabelField(l, label_namespace="labels",
                                 skip_indexing=True) for l in split[-1]]
        else:
            labels = [NumericField(l) for l in split[-1]]

        instances = [Instance({"input1": input1, "input2": input2, \
                     "label": label}) for (input1, input2, label) in
                     zip(inputs1, inputs2, labels)]

    else:
        inputs1 = [TextField(sent, token_indexers=token_indexer) for \
                   sent in split[0]]
        if categorical:
            labels = [LabelField(l, label_namespace="labels",
                                 skip_indexing=True) for l in split[-1]]
        else:
            labels = [NumericField(l) for l in split[-1]]

        instances = [Instance({"input1": input1, \
                     "label": label}) for (input1, label) in
                     zip(inputs1, labels)]
    return Dataset(instances)


def load_word_embeddings(path, vocab, word_dim):
    '''
    Load embeddings from GloVe vectors.

    TODO
        - make this a standard library function.

    Args:
        - path (str): path to word embedding file
        - tok2idx (dict): dictionary mapping words to indices
        - word_dim (int): word vector dimensionality

    Returns
        - embeddings (np.FloatArray): embeddings
    '''
    vocab_size = vocab.get_vocab_size('tokens')
    unk_idx = vocab.get_token_index('@@UNKNOWN@@')
    pad_idx = vocab.get_token_index('@@PADDING@@')
    embeddings = np.random.rand(vocab.get_vocab_size('tokens'),
                                word_dim).astype(float)
    #embeddings = np.zeros((vocab.get_vocab_size('tokens'), word_dim))#.astype(float)
    n_embs = 0
    counter = 0
    with open(path) as fh:
        for row in fh:
            row = row.split()
            if len(row) != word_dim + 1:
                #print("\t\t\tBad example at row %d" % counter)
                word = ' '.join(row[:-word_dim])
            else:
                word = row[0]
            idx = vocab.get_token_index(word)
            if idx != unk_idx:
                embeddings[idx] = np.array([float(v) for v in row[-word_dim:]])
                n_embs += 1
            counter += 1

    '''
    sums = np.nonzero(embeddings.sum(axis=1) == 0)[0]
    no_emb = []
    for idx in sums:
        no_emb.append(vocab.get_token_from_index(idx))
    '''

    embeddings[pad_idx] = 0.
    log.info("\tLoaded pretrained embeddings for %d/%d words",
             n_embs, vocab_size)
    return torch.FloatTensor(embeddings)

def main(arguments):
    '''
    Train or load a model. Evaluate on some tasks.
    '''
    parser = argparse.ArgumentParser(description='')

    parser.add_argument('--cuda', help='0 if no CUDA, else gpu id',
                        type=int, default=0)

    parser.add_argument('--exp_name', help='experiment name',
                        type=str, default='')
    parser.add_argument('--log_file', help='path to log to',
                        type=str, default=0)
    parser.add_argument('--exp_dir', help='path to log to',
                        type=str, default='')
    parser.add_argument('--data_path', help='path to directory containing '
                        ' {train,val,test}.pkl', type=str, default='')
    parser.add_argument('--vocab_path', help='path to directory containing '
                        ' {train,val,test}.pkl', type=str, default='')
    parser.add_argument('--word_embs_file', help='file containing word ' +
                        'embeddings', type=str, default='')

    parser.add_argument('--load_model', help='1 if load from checkpoint',
                        type=int, default=1)
    parser.add_argument('--load_tasks', help='1 if load tasks',
                        type=int, default=1)
    parser.add_argument('--load_vocab', help='1 if load vocabulary',
                        type=int, default=1)
    parser.add_argument('--load_index', help='1 if load indexed datasets',
                        type=int, default=1)

    parser.add_argument('--tasks', help='comma separated list of tasks',
                        type=str, default='')
    parser.add_argument('--max_vocab_size', help='vocabulary size',
                        type=int, default=10000)
    parser.add_argument('--max_seq_len', help='max sequence length',
                        type=int, default=35)
    parser.add_argument('--classifier', help='type of classifier to use',
                        type=str, default='log_reg', choices=['log_reg', 'mlp'])
    parser.add_argument('--classifier_hid_dim', help='hid dim of classifier',
                        type=int, default=512)
    parser.add_argument('--classifier_dropout', help='classifier dropout',
                        type=float, default=0.0)

    parser.add_argument('--max_char_vocab_size', help='char vocabulary size',
                        type=int, default=2000)
    parser.add_argument('--n_char_filters', help='num of conv filters for ' +
                        'char embedding combiner', type=int, default=64)
    parser.add_argument('--char_filter_sizes', help='filter sizes for char' +
                        ' embedding combiner', type=str, default='2,3,4,5')

    parser.add_argument('--pair_enc', help='type of pair encoder to use',
                        type=str, default='bidaf', choices=['simple', 'bidaf'])
    parser.add_argument('--word_dim', help='dimension of word embeddings',
                        type=int, default=300)
    parser.add_argument('--char_dim', help='dimension of char embeddings',
                        type=int, default=100)
    parser.add_argument('--hid_dim', help='hidden dimension size',
                        type=int, default=4096)
    parser.add_argument('--n_layers', help='number of RNN layers',
                        type=int, default=1)
    parser.add_argument('--n_highway_layers', help='num of highway layers',
                        type=int, default=1)

    parser.add_argument('--batch_size', help='batch size',
                        type=int, default=64)
    parser.add_argument('--optimizer', help='optimizer to use',
                        type=str, default='sgd')
    parser.add_argument('--n_epochs', help='n epochs to train for',
                        type=int, default=10)
    parser.add_argument('--lr', help='starting learning rate',
                        type=float, default=1.0)
    parser.add_argument('--scheduler_threshold', help='scheduler threshold',
                        type=float, default=1e-2)

    parser.add_argument('--n_passes_per_epoch', help='Number of passes through'+
                        " each task per epoch. Number of batches per pass " +
                        "is automatically scaled to be even across passes.",
                        type=int, default=1)
    parser.add_argument('--val_metric', help='Single number for early stopping'+
                        '. Must be a task name or "micro" or "macro".',
                        type=str, default='micro')
    parser.add_argument('--task_ordering', help='Method for ordering tasks',
                        type=str, default='given',
                        choices=['given', 'random', 'small_to_large'])


    args = parser.parse_args(arguments)

    log.basicConfig(format='%(asctime)s: %(message)s', level=log.INFO,
                    datefmt='%m/%d/%Y %I:%M:%S %p')
    file_handler = log.FileHandler(args.log_file)
    log.getLogger().addHandler(file_handler)
    log.info(args)

    if args.cuda >= 0:
        log.info("Using GPU %d", args.cuda)
        torch.cuda.set_device(args.cuda)

    ### Load tasks ###
    log.info("Loading tasks...")
    start_time = time.time()
    word_dim, char_dim = args.word_dim, args.char_dim
    input_dim = word_dim + char_dim
    dim = args.hid_dim
    tasks, vocab, word_embs = \
            prepare_tasks(args.tasks.split(','), args.word_embs_file,
                          word_dim, args.max_vocab_size, args.max_seq_len,
                          args.vocab_path, args.exp_dir,
                          args.load_tasks, args.load_vocab, args.load_index)
    log.info('\tFinished loading tasks in %.3fs', time.time() - start_time)

    ### Build model ###
    # Probably should create another function
    word_embedder = Embedding(vocab.get_vocab_size('tokens'), word_dim,
                              weight=word_embs, trainable=False,
                              padding_index=vocab.get_token_index('@@PADDING@@'))
    char_embedder = Embedding(vocab.get_vocab_size('token_characters'),
                              char_dim)
    #char_encoder = BagOfEmbeddingsEncoder(char_dim, True)
    filter_sizes = tuple([int(i) for i in args.char_filter_sizes.split(',')])
    char_encoder = CnnEncoder(args.char_dim, num_filters=args.n_char_filters,
                              ngram_filter_sizes=filter_sizes,
                              output_dim=args.char_dim)
    char_embedder = TokenCharactersEncoder(char_embedder, char_encoder)
    token_embedder = {"words": word_embedder, "chars": char_embedder}
    text_field_embedder = BasicTextFieldEmbedder(token_embedder)
    phrase_layer = s2s_e.by_name('lstm').from_params(Params({
        'input_size': input_dim,
        'hidden_size': dim,
        'bidirectional': True}))
    sent_encoder = HeadlessSentEncoder(vocab, text_field_embedder,
                                       args.n_highway_layers,
                                       phrase_layer)
    if args.pair_enc == 'bidaf':
        modeling_layer = s2s_e.by_name('lstm').from_params(Params({
            'input_size': 8 * dim,
            'hidden_size': dim,
            'num_layers': args.n_layers,
            'bidirectional': True}))
        pair_encoder = HeadlessBiDAF(vocab, text_field_embedder,
                                     args.n_highway_layers,
                                     phrase_layer,
                                     LinearSimilarity(2*dim, 2*dim, "x,y,x*y"),
                                     modeling_layer)
    elif args.pair_enc == 'simple':
        pair_encoder = HeadlessPairEncoder(vocab, text_field_embedder,
                                           args.n_highway_layers,
                                           phrase_layer,
                                           dropout=0.0)
    model = MultiTaskModel(sent_encoder, pair_encoder, args.pair_enc)
    build_classifiers(tasks, model, args.classifier, args.pair_enc, dim,
                      args.classifier_hid_dim, args.classifier_dropout)
    if args.cuda >= 0:
        model = model.cuda()
    log.info('\tFinished building model in %.3fs', time.time() - start_time)

    ### Set up trainer ###
    optimizer_params = Params({'type':args.optimizer, 'lr':args.lr})
    scheduler_params = Params({'type':'reduce_on_plateau', 'mode':'max',
                               'factor':.2, 'patience':0,
                               'threshold':args.scheduler_threshold,
                               'threshold_mode':'abs',
                               'verbose':True})
    iterator = BasicIterator(args.batch_size)

    # Get validation metric
    if args.val_metric in ['micro', 'macro']:
        metric = "+%s_accuracy" % args.val_metric
    else:
        assert args.val_metric in [task.name for task in tasks], \
                "Invalid validation metric"
        task = [task for task in tasks if task.name == args.val_metric][0]
        if task.val_metric_decreases:
            metric = "-"
        else:
            metric = "+"
        metric += task.val_metric

    train_params = Params({'num_epochs':args.n_epochs,
                           'cuda_device':args.cuda,
                           'patience':5,
                           'grad_norm':5.,
                           'val_metric':metric,
                           'lr_decay':.99, 'min_lr':1e-5,
                           'no_tqdm':True})
    trainer = MultiTaskTrainer.from_params(model, args.exp_dir, iterator,
                                           train_params)

    ### Train ###
    trainer.train(tasks, args.task_ordering, args.n_passes_per_epoch,
                  optimizer_params, scheduler_params, args.load_model)

    # Evaluate
    results = evaluate(model, tasks, iterator, cuda_device=args.cuda)
    log.info('*** TEST RESULTS ***')
    for name, value in results.items():
        log.info("%s\t%3f", name, value)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))