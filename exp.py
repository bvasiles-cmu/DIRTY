"""
Variable renaming

Usage:
    exp.py train [options] CONFIG_FILE

Options:
    -h --help                                   Show this screen
    --cuda                                      Use GPU
    --debug                                     Debug mode
    --seed=<int>                                Seed [default: 0]
    --work-dir=<dir>                            work dir [default: data/exp_runs/]
    --extra-config=<str>                              extra config [default: {}]
"""

import random
import time
from typing import List, Tuple, Dict, Iterable
import sys
import numpy as np
import os
import json
import _jsonnet
import pprint
from docopt import docopt
from tqdm import tqdm
import psutil, gc

import torch

from model.decoder import SimpleDecoder
from model.encoder import GraphASTEncoder
from model.gnn import AdjacencyList, GatedGraphNeuralNetwork
from model.model import RenamingModel
from utils import nn_util, util
from utils.ast import AbstractSyntaxTree
from utils.dataset import Dataset, Example
from utils.evaluation import Evaluator
from utils.vocab import Vocab, VocabEntry


import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '2'
# os.environ['GPU_DEBUG'] = '2'
# from gpu_profile import gpu_profile


def train(args):
    work_dir = args['--work-dir']
    config = json.loads(_jsonnet.evaluate_file(args['CONFIG_FILE']))
    config['work_dir'] = work_dir

    if not os.path.exists(work_dir):
        print(f'creating work dir [{work_dir}]', file=sys.stderr)
        os.makedirs(work_dir)

    if args['--extra-config']:
        extra_config = args['--extra-config']
        extra_config = json.loads(extra_config)
        config = util.update(config, extra_config)

    json.dump(config, open(os.path.join(work_dir, 'config.json'), 'w'), indent=2)

    model = RenamingModel.build(config)
    model.train()

    print('Current Configuration:', file=sys.stderr)
    pp = pprint.PrettyPrinter(indent=2, stream=sys.stderr)
    pp.pprint(model.config)

    if args['--cuda']:
        model = model.cuda()

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.Adam(params, lr=0.001)
    nn_util.glorot_init(params)

    # set the padding index for embedding layers to zeros
    # model.encoder.var_node_name_embedding.weight[0].fill_(0.)

    train_set = Dataset(config['data']['train_file'], bpe_model_path=config['data']['bpe_model_path'])
    dev_set = Dataset(config['data']['dev_file'], bpe_model_path=config['data']['bpe_model_path'])
    batch_size = config['train']['batch_size']

    print(f'Training set size {len(train_set)}, dev set size {len(dev_set)}', file=sys.stderr)

    # training loop
    train_iter = epoch = 0
    log_every = config['train']['log_every']
    cum_loss = cum_examples = 0.
    t1 = time.time()

    while True:
        # load training dataset, which is a collection of ASTs and maps of gold-standard renamings
        train_set_iter = train_set.batch_iterator(batch_size=batch_size, progress=True, shuffle=True, num_workers=5,
                                                  filter_func=lambda e: len(e.variable_name_map) > 0 and
                                                                        any(k != v for k, v in e.variable_name_map.items()) and
                                                                        e.ast.size < 300)
        epoch += 1

        for batch_examples in train_set_iter:
            train_iter += 1
            optimizer.zero_grad()

            src_asts = [e.ast for e in batch_examples]
            # print([ast.size for ast in src_asts], file=sys.stderr)
            rename_maps = [e.variable_name_map for e in batch_examples]

            tgt_log_probs, info = model(src_asts, rename_maps)
            # for i in tgt_log_probs:
            #     print(i)
            print(info, file=sys.stderr)

            # for i, (src_ast, rename_map) in enumerate(zip(src_asts, rename_maps)):
            #     log_probs, _info = model([src_ast], [rename_map])
            #
            #     tree_node_encoding_1 = info['context_encoding']['packed_tree_node_encoding']
            #     tree_node_encoding_2 = _info['context_encoding']['packed_tree_node_encoding']
            #
            #     packed_graph_1 = info['context_encoding']['packed_graph']
            #     packed_graph_2 = _info['context_encoding']['packed_graph']
            #
            #     for node_group, nodes in packed_graph_1.node_groups[i].items():
            #         for ast_node, packed_node_id in nodes.items():
            #             encoding1 = tree_node_encoding_1[packed_node_id]
            #             encoding2 = tree_node_encoding_2[packed_graph_2.node_groups[0][node_group][ast_node]]
            #
            #             diff_sum = torch.abs(encoding1 - encoding2).mean()
            #             print(diff_sum.item())
            #             if diff_sum.item() > 1e-6:
            #                 pass

            loss = -tgt_log_probs.mean()

            cum_loss += loss.item()
            cum_examples += len(batch_examples)

            loss.backward()
            # print(loss.item())
            # cpuStats()
            # memReport()

            # clip gradient
            grad_norm = torch.nn.utils.clip_grad_norm_(params, 5.)

            optimizer.step()
            del loss

            if train_iter % log_every == 0:
                print(f'[Learner] train_iter={train_iter} avg. loss={cum_loss / cum_examples}, '
                      f'{cum_examples} batch_examples ({cum_examples / (time.time() - t1)} examples/s)', file=sys.stderr)

                cum_loss = cum_examples = 0.
                t1 = time.time()

        print(f'[Learner] Epoch {epoch} finished', file=sys.stderr)
        t1 = time.time()
        eval_results = Evaluator.decode_and_evaluate(model, dev_set)
        print(f'[Learner] Evaluation result {eval_results} (took {time.time() - t1}s)', file=sys.stderr)
        t1 = time.time()


if __name__ == '__main__':
    cmd_args = docopt(__doc__)

    # seed the RNG
    seed = int(cmd_args['--seed'])
    print(f'use random seed {seed}', file=sys.stderr)
    torch.manual_seed(seed)

    use_cuda = cmd_args['--cuda']
    if use_cuda:
        torch.cuda.manual_seed(seed)
    np.random.seed(seed * 13 // 7)
    random.seed(seed * 17 // 7)

    train(cmd_args)
