import torch
import os
import ctypes
from random import randrange, uniform, seed
import numpy as np
from ..module.model.Model import Model
from .Trainer import Trainer
from .Tester import Tester
from ..data import TestDataLoader
from ..module.strategy import NegativeSampling
from ..module.loss import MarginLoss
from collections import defaultdict
from tqdm import tqdm


def get_string_key(entity, relation):
    return '{},{}'.format(entity, relation)


def defaultdict_int(innerfactory=int):
    return defaultdict(innerfactory)


def float_default():
    return float("inf")


def to_tensor(x, use_gpu):
    if use_gpu:
        return torch.tensor([x]).cuda()
    else:
        return torch.tensor([x])


class Parallel_Universe_Config(Tester):
    def __init__(self,
                 train_dataloader=None, training_identifier='', valid_dataloader=None, test_dataloader=None,
                 initial_num_universes=5000,
                 min_margin=1, max_margin=4, min_lr=0.01, max_lr=0.1, min_num_epochs=50, max_num_epochs=200,
                 const_num_epochs=None, min_triple_constraint=500, max_triple_constraint=2000, min_balance=0.25,
                 max_balance=0.5, embedding_model=None, embedding_model_param=None,
                 missing_embedding_handling='last_rank',
                 save_steps=5, checkpoint_dir='./checkpoint/', valid_steps=5, training_setting="static",
                 incremental_strategy="normal"):
        super(Parallel_Universe_Config, self).__init__(data_loader=test_dataloader, use_gpu=torch.cuda.is_available())

        """ Train data + variables"""
        self.train_dataloader = train_dataloader
        self.ent_tot = train_dataloader.entTotal
        self.rel_tot = train_dataloader.relTotal
        self.training_identifier = training_identifier

        """ "-constant traininghyper parameters" """
        self.embedding_model = embedding_model
        self.embedding_model_param = embedding_model_param

        """ Parallel Universe data structures """
        self.initial_num_universes = initial_num_universes
        self.next_universe_id = 0

        self.trained_embedding_spaces = defaultdict(Model)  # universe_id -> embedding_space
        self.entity_id_mappings = defaultdict(defaultdict_int)  # universe_id -> global entity_id -> local_entity_id
        self.relation_id_mappings = defaultdict(
            defaultdict_int)  # universe_id -> global relation_id -> local_relation_id

        self.entity_universes = defaultdict(set)  # entity_id -> universe_id
        self.relation_universes = defaultdict(set)  # relation_id -> universe_id

        self.initial_random_seed = self.train_dataloader.lib.getRandomSeed()

        """Parallel Universe spans for randomizing embedding space hyper parameters"""
        self.min_margin = min_margin
        self.max_margin = max_margin
        self.min_lr = min_lr
        self.max_lr = max_lr
        self.min_num_epochs = min_num_epochs
        self.max_num_epochs = max_num_epochs
        self.const_num_epochs = const_num_epochs
        self.min_triple_constraint = min_triple_constraint
        self.max_triple_constraint = max_triple_constraint
        self.min_balance = min_balance
        self.max_balance = max_balance

        """ saving """
        self.save_steps = save_steps
        self.checkpoint_dir = checkpoint_dir

        """ Eval """
        self.missing_embedding_handling = missing_embedding_handling

        """ ""Valid"" """
        self.lib.validHead.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        self.lib.validTail.argtypes = [ctypes.c_void_p, ctypes.c_int64]
        self.lib.getValidHit10.restype = ctypes.c_float

        self.valid_dataloader = valid_dataloader if valid_dataloader else TestDataLoader(train_dataloader.in_path,
                                                                                         "link", mode='valid')

        self.valid_steps = valid_steps
        self.early_stopping_patience = 10
        self.bad_counts = 0
        self.best_hit10 = 0

        """Global Energy Estimation data structures"""
        self.current_tested_universes = 0
        self.current_validated_universes = 0
        self.evaluation_head2tail_triple_score_dict = {}
        self.evaluation_tail2head_triple_score_dict = {}
        self.evaluation_head2rel_tuple_score_dict = {}
        self.evaluation_tail2rel_tuple_score_dict = {}
        self.default_scores = [float("inf") for i in range(self.ent_tot)]

        self.training_setting = training_setting  # ["incremental" | "static"]
        self.incremental_strategy  = incremental_strategy  # ["normal" | "deprecate"]
        if self.training_setting=="incremental" and self.incremental_strategy == "deprecate":
            self.deprecated_embeddingspaces = set()

    def get_default_value_list(self):
        return [float("inf") for i in range(self.ent_tot)]

    def set_random_seed(self, rand_seed):
        self.train_dataloader.lib.setRandomSeed(rand_seed)
        self.train_dataloader.lib.randReset()
        seed(rand_seed)
        torch.manual_seed(rand_seed)

    def set_valid_dataloader(self, valid_dataloader):
        self.valid_dataloader = valid_dataloader

    def set_test_dataloader(self, test_dataloader):
        self.data_loader = test_dataloader

    def embedding_model_factory(self, ent_tot, rel_tot, margin):
        embedding_method = self.embedding_model(ent_tot, rel_tot, **self.embedding_model_param)

        return NegativeSampling(
            model=embedding_method,
            loss=MarginLoss(margin=margin),
            batch_size=self.train_dataloader.batch_size
        )

    def valid(self):
        self.lib.validInit()
        validation_range = tqdm(self.valid_dataloader)
        for index, [valid_head_batch, valid_tail_batch] in enumerate(validation_range):
            score = self.global_energy_estimation(valid_head_batch)
            self.lib.validHead(score.__array_interface__["data"][0], index)
            score = self.global_energy_estimation(valid_tail_batch)
            self.lib.validTail(score.__array_interface__["data"][0], index)
        return self.lib.getValidHit10()

    def process_universe_mappings(self):
        entity_remapping, relation_remapping = self.train_dataloader.get_universe_mappings()

        entity_total_universe = self.train_dataloader.lib.getEntityTotalUniverse()
        print('Entities are %d' % entity_total_universe)
        for entity in range(entity_total_universe):
            self.entity_universes[entity_remapping[entity].item()].add(self.next_universe_id)
            self.entity_id_mappings[self.next_universe_id][entity_remapping[entity].item()] = entity

        relation_total_universe = self.train_dataloader.lib.getRelationTotalUniverse()
        for relation in range(relation_total_universe):
            self.relation_universes[relation_remapping[relation].item()].add(self.next_universe_id)
            self.relation_id_mappings[self.next_universe_id][relation_remapping[relation].item()] = relation

    def compile_train_datset(self):
        # Create train dataset for universe and process mapping of contained global entities and relations
        triple_constraint = randrange(self.min_triple_constraint, self.max_triple_constraint)
        balance_param = round(uniform(self.min_balance, self.max_balance), 2)

        # Outsourced sampling of relation to C++ getParallelUniverse in file
        # UniverseConstructor.h (l.291 - l.299)
        # relation_in_focus = randrange(0, self.train_dataloader.relTotal - 1)

        print('universe information-------------------')
        print('--- num of training triples: %d' % triple_constraint)
        self.train_dataloader.compile_universe_dataset(triple_constraint, balance_param)
        self.process_universe_mappings()
        print('--- num of universe entities: %d' % self.train_dataloader.lib.getEntityTotalUniverse())
        print('--- num of universe relations: %d' % self.train_dataloader.lib.getRelationTotalUniverse())
        print('---------------------------------------')

        print('Train dataset for embedding space compiled.')

    def train_embedding_space(self):
        # Create Model with factory
        entity_total_universe = self.train_dataloader.lib.getEntityTotalUniverse()
        relation_total_universe = self.train_dataloader.lib.getRelationTotalUniverse()
        margin = randrange(self.min_margin, self.max_margin)
        model = self.embedding_model_factory(ent_tot=entity_total_universe, rel_tot=relation_total_universe,
                                             margin=margin)

        # Initialize Trainer
        train_times = self.const_num_epochs if self.const_num_epochs is not None \
            else randrange(self.min_num_epochs, self.max_num_epochs)

        lr = round(uniform(self.min_lr, self.max_lr), len(str(self.min_lr).split('.')[1]))
        trainer = Trainer(model=model, data_loader=self.train_dataloader, train_times=train_times, alpha=lr,
                          use_gpu=self.use_gpu, opt_method='Adagrad')

        print('hyperparams for universe %d------------' % self.next_universe_id)
        print('--- epochs: %d' % train_times)
        print('--- learning rate:', lr)
        print('--- margin: %d' % margin)
        print('--- norm: %d' % self.embedding_model_param['p_norm'])
        print('--- dimensions: %d' % self.embedding_model_param['dim'])

        # Train embedding space
        self.train_dataloader.swap_helpers()
        trainer.run()
        self.train_dataloader.reset_universe()

        return model.model

    def add_embedding_space(self, embedding_space):
        for param in embedding_space.parameters():
            param.requires_grad = False

        self.trained_embedding_spaces[self.next_universe_id] = embedding_space

    def save_model(self):
        self.save_parameters(
            os.path.join(
                '{}Pu{}_learned_spaces-{}_{}.ckpt'.format(self.checkpoint_dir, self.embedding_model.__name__,
                                                          self.next_universe_id,
                                                          self.training_identifier)))

    def train_parallel_universes(self, num_of_embedding_spaces):
        for universe_id in range(num_of_embedding_spaces):
            self.set_random_seed(self.initial_random_seed + self.next_universe_id)
            self.compile_train_datset()
            embedding_space = self.train_embedding_space()
            self.add_embedding_space(embedding_space)
            self.next_universe_id += 1

            if (universe_id + 1) % self.valid_steps == 0:
                print("Universe %d has finished, validating..." % (self.next_universe_id - 1))
                self.eval_universes(eval_mode='valid')
                hit10 = self.valid()
                if hit10 > self.best_hit10:
                    self.best_hit10 = hit10
                    print("Best model | hit@10 of valid set is %f" % self.best_hit10)
                    print('Save model at universe %d.' % self.next_universe_id)
                    self.save_model()
                    self.bad_counts = 0
                else:
                    print(
                        "Hit@10 of valid set is %f | bad count is %d"
                        % (hit10, self.bad_counts)
                    )
                    self.bad_counts += 1
                if self.bad_counts == self.early_stopping_patience:
                    print("Early stopping at universe {}".format(self.next_universe_id - 1))
                    break

            if self.save_steps and self.checkpoint_dir and (universe_id + 1) % self.save_steps == 0:
                print('Save model at universe %d.' % self.next_universe_id)
                self.save_model()

    def gather_embedding_spaces(self, entity_1, rel, entity_2=None):
        entity_occurences = self.entity_universes[entity_1]
        relation_occurences = self.relation_universes[rel]
        embedding_space_ids = entity_occurences.intersection(relation_occurences)
        if entity_2 != None:
            entity2_occurences = self.entity_universes[entity_2]
            embedding_space_ids = embedding_space_ids.intersection(entity2_occurences)
        return embedding_space_ids

    def predict_triple(self, head_id, rel_id, tail_id, mode='normal'):
        # Gather embedding spaces in which the triple is hold
        embedding_space_ids = self.gather_embedding_spaces(head_id, rel_id, tail_id)

        # Iterate through spaces and get collect the max energy score
        min_energy_score = float("inf")
        for embedding_space_id in embedding_space_ids:
            embedding_space = self.trained_embedding_spaces[embedding_space_id]

            local_head_id = self.entity_id_mappings[embedding_space_id][head_id]
            local_head_id = to_tensor(local_head_id, self.use_gpu)
            local_tail_id = self.entity_id_mappings[embedding_space_id][tail_id]
            local_tail_id = to_tensor(local_tail_id, self.use_gpu)
            local_rel_id = self.relation_id_mappings[embedding_space_id][rel_id]
            local_rel_id = to_tensor(local_rel_id, self.use_gpu)

            energy_score = embedding_space.predict(
                {'batch_h': local_head_id,
                 'batch_t': local_tail_id,
                 'batch_r': local_rel_id,
                 'mode': mode
                 }
            )

            if energy_score < min_energy_score:
                min_energy_score = energy_score

        return min_energy_score

    def calc_tuple_score(self, ent_id, rel_id, mode, embedding_space):
        rel_embedding = embedding_space.rel_embeddings(to_tensor(rel_id, use_gpu=self.use_gpu))
        ent = embedding_space.ent_embeddings(to_tensor(ent_id, use_gpu=self.use_gpu))
        zero_vec = to_tensor([0.0], use_gpu=self.use_gpu)
        if mode == 'head_batch':
            head_embedding = zero_vec
            tail_embedding = ent
        elif mode == 'tail_batch':
            head_embedding = ent
            tail_embedding = zero_vec
        return embedding_space._calc(head_embedding, tail_embedding, rel_embedding, mode)

    def transmit_max_scores(self, data, embedding_space_mapping, scores):
        mode = data['mode']
        eval_rel_id = data['batch_r'][0]

        if mode == 'head_batch':
            eval_entity_id = data['batch_t'][0] if mode == 'head_batch' else data['batch_h'][0]
            score_dict = self.evaluation_tail2head_triple_score_dict

        elif mode == 'tail_batch':
            eval_entity_id = data['batch_t'][0] if mode == 'head_batch' else data['batch_h'][0]
            score_dict = self.evaluation_head2tail_triple_score_dict

        dict_key = get_string_key(eval_entity_id, eval_rel_id)
        global_energy_scores = score_dict.setdefault(dict_key, self.default_scores.copy())

        for global_entity_id, local_entity_id in embedding_space_mapping.items():
            entity_score = scores[local_entity_id].item()

            if entity_score < global_energy_scores[global_entity_id]:
                global_energy_scores[global_entity_id] = entity_score

            # def transmit_max_scores(self, data, embedding_space_mapping, scores):
            #     mode = data['mode']
            #     eval_entity_id = data['batch_t'][0] if mode == 'head_batch' else data['batch_h'][0]
            #     eval_rel_id = data['batch_r'][0]
            #
            #     for global_entity_id, local_entity_id in embedding_space_mapping.items():
            #         if mode == 'head_batch'
            #             dict_key = (global_entity_id, eval_rel_id)
            #             entity = eval_entity_id
            #         elif mode == 'tail_batch':
            #             dict_key = (eval_entity_id, eval_rel_id)
            #             entity = global_entity_id
            #
            #         triple_scores = self.evaluation_triple_score_dict[dict_key]
            #         entity_score = scores[local_entity_id]
            #         # get_string_key(global_entity_id, eval_rel_id, eval_entity_id) if mode == 'head_batch' \
            #         # else get_string_key(eval_entity_id, eval_rel_id, global_entity_id)
            #
            #         if not triple_scores:
            #             triple_scores.append(entity, entity_score)
            #
            #         if not triple_scores:
            #             pairs[key].append(c + ':' + freq if freq != '1' else c)
            #
            #         elif entity_score.item() < BinSearch(triple_scores, entity):
            #             self.evaluation_triple_score_dict[min_dict_key] = entity_score.item()

    def transmit_tuple_max_score(self, data, universe_id):
        mode = data["mode"]
        eval_rel_id = data['batch_r'][0]

        if mode == 'head_batch':
            eval_entity_id = data['batch_t'][0] if mode == 'head_batch' else data['batch_h'][0]
            score_dict = self.evaluation_tail2rel_tuple_score_dict

        elif mode == 'tail_batch':
            eval_entity_id = data['batch_t'][0] if mode == 'head_batch' else data['batch_h'][0]
            score_dict = self.evaluation_head2rel_tuple_score_dict

        dict_key = get_string_key(eval_entity_id, eval_rel_id)

        embedding_space = self.trained_embedding_spaces[universe_id]
        local_entity_id = self.entity_id_mappings[universe_id][eval_entity_id]
        local_relation_id = self.relation_id_mappings[universe_id][eval_rel_id]

        local_tuple_score = self.calc_tuple_score(local_entity_id, local_relation_id, mode, embedding_space)
        if local_tuple_score < score_dict.get(dict_key, float_default()):
            score_dict[dict_key] = local_tuple_score

    def obtain_embedding_space_score(self, data, universe_id):
        batch_h = data['batch_h']
        batch_t = data['batch_t']
        batch_r = data['batch_r']
        mode = data['mode']

        embedding_space_mapping = self.entity_id_mappings[universe_id]
        embedding_space = self.trained_embedding_spaces[universe_id]

        local_batch_h = embedding_space_mapping.keys() if mode == "head_batch" else batch_h
        local_batch_h = [self.entity_id_mappings[universe_id][global_entity_id] for global_entity_id in
                         local_batch_h]
        local_batch_t = batch_t if mode == "head_batch" else embedding_space_mapping.keys()
        local_batch_t = [self.entity_id_mappings[universe_id][global_entity_id] for global_entity_id in
                         local_batch_t]
        local_batch_r = [self.relation_id_mappings[universe_id][global_relation_id] for global_relation_id in
                         batch_r]

        embedding_space_scores = embedding_space.predict(
            {"batch_h": to_tensor(local_batch_h, use_gpu=self.use_gpu),
             "batch_t": to_tensor(local_batch_t, use_gpu=self.use_gpu),
             "batch_r": to_tensor(local_batch_r, use_gpu=self.use_gpu),
             "mode": mode
             }
        )
        # iterate through dict and score tensor (index of both is equal) and transmit scores with comparison to energy_scores
        self.transmit_max_scores(data, embedding_space_mapping, embedding_space_scores)

        if self.missing_embedding_handling == 'null_vector':
            self.transmit_tuple_max_score(data, universe_id)

    def eval_universes(self, eval_mode):
        eval_dataloader = self.data_loader if eval_mode == 'test' else self.valid_dataloader
        evaluation_range = tqdm(eval_dataloader)

        current_evaluated_universes = self.current_tested_universes if eval_mode == 'test' else self.current_validated_universes

        eval_embeddingspaces = None
        if self.training_setting == "incremental":
            # Reset triple and tuple scores because train and valid data changes along snapshots
            self.evaluation_head2tail_triple_score_dict.clear()
            self.evaluation_tail2head_triple_score_dict.clear()
            self.evaluation_head2rel_tuple_score_dict.clear()
            self.evaluation_tail2rel_tuple_score_dict.clear()

            eval_embeddingspaces = range(0, self.next_universe_id)

            # If strategy is "deprecate", deprecate embedding spaces in which deleted triples occur by restricting
            # evaluation range
            if self.incremental_strategy == "deprecate":
                self.determine_deprecated_embedding_spaces()
                eval_embeddingspaces = [embedding_space for embedding_space in range(0, self.next_universe_id)
                                        if embedding_space not in self.deprecated_embeddingspaces]


        elif self.training_setting == "static":
            eval_embeddingspaces = range(current_evaluated_universes, self.next_universe_id)


        for index, [data_head, data_tail] in enumerate(evaluation_range):
            head = data_tail['batch_h'][0]
            rel = data_head['batch_r'][0]
            tail = data_head['batch_t'][0]

            for universe_id in eval_embeddingspaces:
                if (universe_id in self.entity_universes[head]) and (universe_id in self.relation_universes[rel]):
                    self.obtain_embedding_space_score(data_tail, universe_id)

                if (universe_id in self.entity_universes[tail]) and (universe_id in self.relation_universes[rel]):
                    self.obtain_embedding_space_score(data_head, universe_id)

        if eval_mode == 'test':
            self.current_tested_universes = self.next_universe_id
        elif eval_mode == 'valid':
            self.current_validated_universes = self.next_universe_id

    def global_energy_estimation(self, data):
        batch_h = data['batch_h']
        batch_t = data['batch_t']
        batch_r = data['batch_r']
        eval_rel_id = batch_r[0]
        mode = data['mode']

        if mode == 'head_batch':
            eval_entity_id = batch_t[0]
            evaluation_entities = batch_h
            num_of_evaluation_entitites = len(batch_h)
            score_dict = self.evaluation_tail2head_triple_score_dict

        elif mode == 'tail_batch':
            eval_entity_id = batch_h[0]
            evaluation_entities = batch_t
            num_of_evaluation_entitites = len(batch_t)  # For incremental Setting num of currently contained entities
            score_dict = self.evaluation_head2tail_triple_score_dict

        batch_scores = np.zeros(shape=num_of_evaluation_entitites, dtype=np.float32)
        dict_key = get_string_key(eval_entity_id, eval_rel_id)
        score_dict = score_dict.get(dict_key, [])

        if not score_dict:
            batch_scores[:] = float_default()
        else:
            for idx, entity in enumerate(evaluation_entities):
                batch_scores[idx] = score_dict[entity]

        if self.missing_embedding_handling == 'null_vector':
            tuple_score_dict = self.evaluation_tail2rel_tuple_score_dict if mode == 'head_batch' \
                else self.evaluation_head2rel_tuple_score_dict
            missing_value_replacement = tuple_score_dict.get(dict_key, float_default())

            if missing_value_replacement != float_default():
                batch_scores[batch_scores == float_default()] = missing_value_replacement

        # TODO batch_scores (length of evaluation batches which is length of currently contained entities) and
        return batch_scores

    def global_energy_estimation2(self, data):
        batch_h = data['batch_h'].numpy() if type(data['batch_h']) == torch.Tensor else data['batch_h']
        batch_t = data['batch_t'].numpy() if type(data['batch_t']) == torch.Tensor else data['batch_t']
        batch_r = data['batch_r'].numpy() if type(data['batch_r']) == torch.Tensor else data['batch_r']
        mode = data['mode']

        triple_entity = batch_t[0] if mode == 'head_batch' else batch_h[0]
        triple_relation = batch_r[0]
        evaluation_entities = batch_h if mode == 'head_batch' else batch_t

        # Gather embedding spaces in which the tuple (entity, relation) is hold
        embedding_space_ids = self.gather_embedding_spaces(triple_entity, triple_relation)

        energy_scores_dict = defaultdict(float)
        default_value = float("inf")
        for entity in evaluation_entities:
            energy_scores_dict[entity] = default_value
        tuple_score = default_value

        for embedding_space_id in embedding_space_ids:
            # Get list with entities which are embedded in this space
            embedding_space_entity_dict = self.entity_id_mappings[embedding_space_id]
            # Calculate scores with embedding_space.predict({batch_h,batch_r,batch_t, mode})
            embedding_space = self.trained_embedding_spaces[embedding_space_id]

            local_batch_h = embedding_space_entity_dict.keys() if mode == "head_batch" else batch_h
            local_batch_h = [self.entity_id_mappings[embedding_space_id][global_entity_id] for global_entity_id in
                             local_batch_h]
            local_batch_t = batch_t if mode == "head_batch" else embedding_space_entity_dict.keys()
            local_batch_t = [self.entity_id_mappings[embedding_space_id][global_entity_id] for global_entity_id in
                             local_batch_t]

            local_batch_r = [self.relation_id_mappings[embedding_space_id][global_relation_id] for global_relation_id in
                             batch_r]

            embedding_space_scores = embedding_space.predict(
                {"batch_h": to_tensor(local_batch_h, use_gpu=self.use_gpu),
                 "batch_t": to_tensor(local_batch_t, use_gpu=self.use_gpu),
                 "batch_r": to_tensor(local_batch_r, use_gpu=self.use_gpu),
                 "mode": mode
                 }
            )
            # iterate through dict and score tensor (index of both is equal) and transmit scores with comparison to energy_scores
            for global_entity_id, local_entity_id in embedding_space_entity_dict.items():
                entity_score = embedding_space_scores[local_entity_id]
                if entity_score < energy_scores_dict[global_entity_id]:
                    energy_scores_dict[global_entity_id] = entity_score

            if self.missing_embedding_handling == 'null_vector':
                local_batch_ent = local_batch_t if mode == "head_batch" else local_batch_h
                local_tuple_score = self.calc_tuple_score(local_batch_ent, local_batch_r, mode, embedding_space)
                if local_tuple_score < tuple_score:
                    tuple_score = local_tuple_score

        scores = np.fromiter(energy_scores_dict.values(), dtype=np.float32)
        if self.missing_embedding_handling == 'null_vector' and tuple_score != default_value:
            scores[scores == default_value] = tuple_score

        return scores

    def test_one_step(self, data):
        batch_h = data['batch_h']
        batch_t = data['batch_t']
        batch_r = data['batch_r']
        mode = data['mode']

        if mode == 'head_batch' or mode == 'tail_batch':
            score = self.global_energy_estimation(data)

        elif mode == 'normal':
            num_of_scores = batch_h.size()[0]
            score = torch.zeros(num_of_scores)
            for index in range(num_of_scores):
                score[index] = self.predict_triple(batch_h[index], batch_r[index], batch_t[index], mode)

        return score

    def run_link_prediction(self, type_constrain=False):
        self.eval_universes(eval_mode='test')
        mrr, mr, hit10, hit3, hit1 = super().run_link_prediction(type_constrain)
        print('Mean Reciprocal Rank: {}'.format(mrr))
        print('Mean Rank: {}'.format(mr))
        print('Hits@10: {}'.format(hit10))
        print('Hits@3: {}'.format(hit3))
        print('Hits@1: {}'.format(hit1))

    def run_triple_classification(self, threshlod=None):
        self.eval_universes(eval_mode='test')
        acc, _ = super().run_triple_classification(threshlod)
        print("Accuracy is: {}".format(acc))

    # def forward(self, data: dict):
    #     batch_h = data['batch_h']
    #     batch_t = data['batch_t']
    #     batch_r = data['batch_r']
    #     mode = data['mode']
    #
    #     if mode == 'head_batch' or mode == 'tail_batch':
    #         score = self.global_energy_estimation(data)
    #
    #     elif mode == 'normal':
    #         num_of_scores = batch_h.size()[0]
    #         score = torch.zeros(num_of_scores)
    #         for index in range(num_of_scores):
    #             score[index] = self.predict_triple(batch_h[index], batch_r[index], batch_t[index], mode)
    #
    #     return score

    # def predict(self, data: dict):
    #     score = self.forward(data)
    #     return score

    def determine_deprecated_embedding_spaces(self):
        self.deprecated_embeddingspaces.clear()
        for triple in self.train_dataloader.deleted_triple_set():
            head, tail, rel = triple
            embedding_space_ids_set = self.gather_embedding_spaces(head, rel, tail)
            self.deprecated_embeddingspaces.update(embedding_space_ids_set)

    def extend_parallel_universe(self, ParallelUniverse_inst):
        # shift indexes of trained embedding spaces in parameter instance to add them to this instance
        for universe_id in list(ParallelUniverse_inst.trained_embedding_spaces.keys()):
            ParallelUniverse_inst.trained_embedding_spaces[
                universe_id + self.next_universe_id] = ParallelUniverse_inst.trained_embedding_spaces.pop(universe_id)
        self.trained_embedding_spaces.update(ParallelUniverse_inst.trained_embedding_spaces)

        for entity in range(ParallelUniverse_inst.ent_tot):
            self.entity_universes[entity].update(
                ParallelUniverse_inst.entity_universes[entity])  # entity_id -> universe_id

        for relation in range(ParallelUniverse_inst.rel_tot):
            self.relation_universes[relation].update(
                ParallelUniverse_inst.relation_universes[relation])  # relation_id -> universe_id

        for instance_next_universe_id in range(ParallelUniverse_inst.next_universe_id):
            for entity_key in list(ParallelUniverse_inst.entity_id_mappings[instance_next_universe_id].keys()):
                self.entity_id_mappings[self.next_universe_id + instance_next_universe_id][entity_key] = \
                    ParallelUniverse_inst.entity_id_mappings[instance_next_universe_id][entity_key]

            for relation_key in list(ParallelUniverse_inst.relation_id_mappings[instance_next_universe_id].keys()):
                self.relation_id_mappings[self.next_universe_id + instance_next_universe_id][relation_key] = \
                    ParallelUniverse_inst.relation_id_mappings[instance_next_universe_id][relation_key]
                # universe_id -> global entity_id -> universe entity_id

        self.next_universe_id += ParallelUniverse_inst.next_universe_id

    def extend_state_dict(self):
        state_dict = {'initial_num_universes': self.initial_num_universes,
                      'next_universe_id': self.next_universe_id,
                      'trained_embedding_spaces': self.trained_embedding_spaces,
                      'entity_id_mappings': self.entity_id_mappings,
                      'relation_id_mappings': self.relation_id_mappings,
                      'entity_universes': self.entity_universes,
                      'relation_universes': self.relation_universes,
                      'min_margin': self.min_margin,
                      'max_margin': self.max_margin,
                      'min_lr': self.min_lr,
                      'max_lr': self.max_lr,
                      'min_num_epochs': self.min_num_epochs,
                      'max_num_epochs': self.max_num_epochs,
                      'min_triple_constraint': self.min_triple_constraint,
                      'max_triple_constraint': self.max_triple_constraint,

                      'min_balance': self.min_balance,
                      'max_balance': self.max_balance,

                      'embedding_model': self.embedding_model,
                      'embedding_model_param': self.embedding_model_param,

                      'best_hit10': self.best_hit10,
                      'bad_counts': self.bad_counts,

                      'current_tested_universes': self.current_tested_universes,
                      'current_validated_universes': self.current_validated_universes,
                      'evaluation_head2tail_triple_score_dict': self.evaluation_head2tail_triple_score_dict,
                      'evaluation_tail2head_triple_score_dict': self.evaluation_tail2head_triple_score_dict,
                      'evaluation_head2rel_tuple_score_dict': self.evaluation_head2rel_tuple_score_dict,
                      'evaluation_tail2rel_tuple_score_dict': self.evaluation_tail2rel_tuple_score_dict,
                      }

        return state_dict

    def process_state_dict(self, state_dict):
        self.initial_num_universes = state_dict['initial_num_universes']
        self.next_universe_id = state_dict['next_universe_id']
        self.trained_embedding_spaces = state_dict['trained_embedding_spaces']
        self.entity_id_mappings = state_dict['entity_id_mappings']
        self.relation_id_mappings = state_dict['relation_id_mappings']
        self.entity_universes = state_dict['entity_universes']
        self.relation_universes = state_dict['relation_universes']
        self.min_margin = state_dict['min_margin']
        self.max_margin = state_dict['max_margin']
        self.min_lr = state_dict['min_lr']
        self.max_lr = state_dict['max_lr']
        self.min_num_epochs = state_dict['min_num_epochs']
        self.max_num_epochs = state_dict['max_num_epochs']
        self.min_triple_constraint = state_dict['min_triple_constraint']
        self.max_triple_constraint = state_dict['max_triple_constraint']
        if 'min_balance' in state_dict:
            self.min_balance = state_dict['min_balance']
            self.max_balance = state_dict['max_balance']
        # if 'num_dim' in state_dict:
        #     self.num_dim = state_dict['num_dim']
        #     self.norm = state_dict['p_norm']
        # if 'embedding_method' in state_dict:
        #     self.embedding_method = state_dict['embedding_method']
        elif 'embedding_model' in state_dict:
            self.embedding_model = state_dict['embedding_model']
            self.embedding_model_param = state_dict['embedding_model_param']
        if 'best_hit10' in state_dict:
            self.best_hit10 = state_dict['best_hit10']
            self.bad_counts = state_dict['bad_counts']

        if 'current_tested_universes' in state_dict:
            self.current_tested_universes = state_dict['current_tested_universes']
            self.current_validated_universes = state_dict['current_validated_universes']
            self.evaluation_head2tail_triple_score_dict = state_dict['evaluation_head2tail_triple_score_dict']
            self.evaluation_tail2head_triple_score_dict = state_dict['evaluation_tail2head_triple_score_dict']
            self.evaluation_head2rel_tuple_score_dict = state_dict['evaluation_head2rel_tuple_score_dict']
            self.evaluation_tail2rel_tuple_score_dict = state_dict['evaluation_tail2rel_tuple_score_dict']

    def save_parameters(self, path):
        state_dict = self.extend_state_dict()
        torch.save(state_dict, path)

    def load_parameters(self, path):
        state_dict = torch.load(self.checkpoint_dir + path)
        self.process_state_dict(state_dict)

    def calculate_unembedded_ratio(self, mode='examine_entities'):
        num_unembedded = 0
        mapping_dict = self.entity_universes if mode == 'examine_entities' else self.relation_universes
        num_total = self.train_dataloader.entTotal if mode == 'examine_entities' else self.train_dataloader.relTotal

        for i in range(num_total):
            if len(mapping_dict[i]) == 0:
                num_unembedded += 1

        return num_unembedded / num_total
