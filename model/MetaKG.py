import dgl
import torch
import torch.nn as nn
import torch.nn.functional as F
from dgl.nn.pytorch.softmax import edge_softmax
from utility.helper import edge_softmax_fix

def _L2_loss_mean(x, multi=False):
    if multi:
        return torch.mean(torch.sum(torch.pow(x, 2), dim=1, keepdim=False) / 2.)
    else:
        return torch.mean(torch.sum(torch.pow(x, 2)) / 2.)

class Aggregator(nn.Module):

    def __init__(self, in_dim, out_dim, dropout, aggregator_type):
        super(Aggregator, self).__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.dropout = dropout
        self.aggregator_type = aggregator_type

        self.message_dropout = nn.Dropout(dropout)

        if aggregator_type == 'gcn':
            self.W = nn.Linear(self.in_dim, self.out_dim)
        elif aggregator_type == 'graphsage':
            self.W = nn.Linear(self.in_dim * 2, self.out_dim)
        elif aggregator_type == 'bi-interaction':
            self.W1 = nn.Linear(self.in_dim, self.out_dim)
            self.W2 = nn.Linear(self.in_dim, self.out_dim)
        else:
            raise NotImplementedError

        self.activation = nn.LeakyReLU()

    def forward(self, mode, g, entity_embed, vars_dict=None, i=0):
        g = g.local_var()
        g.ndata['node'] = entity_embed

        if mode == 'predict':
            g.update_all(dgl.function.u_mul_e('node', 'att', 'side'), lambda nodes: {'N_h': torch.sum(nodes.mailbox['side'], 1)})
        else:
            g.update_all(dgl.function.u_mul_e('node', 'att', 'side'), dgl.function.sum('side', 'N_h'))

        if self.aggregator_type == 'gcn':
            out = self.activation(self.W(g.ndata['node'] + g.ndata['N_h']))                         # (n_users + n_entities, out_dim)

        elif self.aggregator_type == 'graphsage':
            out = self.activation(self.W(torch.cat([g.ndata['node'], g.ndata['N_h']], dim=1)))      # (n_users + n_entities, out_dim)

        elif self.aggregator_type == 'bi-interaction':
            if vars_dict is None:
                out1 = self.activation(self.W1(g.ndata['node'] + g.ndata['N_h']))                       # (n_users + n_entities, out_dim)
                out2 = self.activation(self.W2(g.ndata['node'] * g.ndata['N_h']))                       # (n_users + n_entities, out_dim)
                out = out1 + out2
            else:
                w1 = vars_dict[str(i)+'_W1_w']
                b1 = vars_dict[str(i)+'_W1_b']
                w2 = vars_dict[str(i)+'_W2_w']
                b2 = vars_dict[str(i)+'_W2_b']
                out1 = self.activation(F.linear(g.ndata['node'] + g.ndata['N_h'], w1, b1))
                out2 = self.activation(F.linear(g.ndata['node'] * g.ndata['N_h'], w2, b2))
                out = out1 + out2

        else:
            raise NotImplementedError

        out = self.message_dropout(out)
        return out


class MetaKG(nn.Module):

    def __init__(self, args,
                 n_users, n_entities, n_relations,
                 user_pre_embed=None, item_pre_embed=None):

        super(MetaKG, self).__init__()
        self.use_pretrain = args.use_pretrain
        self.local_lr = args.local_lr

        self.n_users = n_users
        self.n_entities = n_entities
        self.n_relations = n_relations

        self.entity_dim = args.entity_dim
        self.relation_dim = args.relation_dim

        self.aggregation_type = args.aggregation_type
        self.conv_dim_list = [args.entity_dim] + eval(args.conv_dim_list)
        self.mess_dropout = eval(args.mess_dropout)
        self.n_layers = len(eval(args.conv_dim_list))

        self.kg_l2loss_lambda = args.kg_l2loss_lambda
        self.cf_l2loss_lambda = args.cf_l2loss_lambda

        self.relation_embed = nn.Embedding(self.n_relations, self.relation_dim)
        self.entity_user_embed = nn.Embedding(self.n_entities + self.n_users, self.entity_dim)

        if (self.use_pretrain == 1) and (user_pre_embed is not None) and (item_pre_embed is not None):
            other_entity_embed = nn.Parameter(torch.Tensor(self.n_entities - item_pre_embed.shape[0], self.entity_dim))
            nn.init.xavier_uniform_(other_entity_embed, gain=nn.init.calculate_gain('relu'))
            entity_user_embed = torch.cat([item_pre_embed, other_entity_embed, user_pre_embed], dim=0)
            self.entity_user_embed.weight = nn.Parameter(entity_user_embed)

        self.W_R = nn.Parameter(torch.Tensor(self.n_relations, self.entity_dim, self.relation_dim))
        nn.init.xavier_uniform_(self.W_R, gain=nn.init.calculate_gain('relu'))

        # n-layer aggregator function
        self.aggregator_layers = nn.ModuleList()
        for k in range(self.n_layers):
            self.aggregator_layers.append(Aggregator(self.conv_dim_list[k], self.conv_dim_list[k + 1], self.mess_dropout[k], self.aggregation_type))

    def copy_W_parameters(self):
        return self.W_R

    def copy_agg_parameters(self):
        self.vars = torch.nn.ParameterDict()

        for i, layer in enumerate(self.aggregator_layers):
            self.vars[str(i)+'_W1_w'] = layer.W1.weight
            self.vars[str(i)+'_W1_b'] = layer.W1.bias
            self.vars[str(i)+'_W2_w'] = layer.W2.weight
            self.vars[str(i)+'_W2_b'] = layer.W2.bias

        return self.vars

    def att_score(self, edges):
        r_mul_t = torch.matmul(self.entity_user_embed(edges.src['id']), self.W_r)                       # (n_edge, relation_dim)
        r_mul_h = torch.matmul(self.entity_user_embed(edges.dst['id']), self.W_r)                       # (n_edge, relation_dim)
        # r_mul_h = torch.matmul(self.entity_user_embed(edges.src['id']), self.W_r)
        # r_mul_t = torch.matmul(self.entity_user_embed(edges.dst['id']), self.W_r)
        r_embed = self.relation_embed(edges.data['type'])                                               # (1, relation_dim)
        att = torch.bmm(r_mul_t.unsqueeze(1), torch.tanh(r_mul_h + r_embed).unsqueeze(2)).squeeze(-1)   # (n_edge, 1)
        return {'att': att}


    def compute_attention(self, g):
        g = g.local_var()
        for i in range(self.n_relations):
            edge_idxs = g.filter_edges(lambda edge: edge.data['type'] == i)  # return the index of edge that edge.data['type'] == i
            self.W_r = self.W_R[i]
            g.apply_edges(self.att_score, edge_idxs)

        g.edata['att'] = edge_softmax_fix(g, g.edata.pop('att'))
        return g.edata.pop('att')


    def calc_kg_loss(self, h, r, pos_t, neg_t):
        """
        h:      (kg_batch_size)
        r:      (kg_batch_size)
        pos_t:  (kg_batch_size)
        neg_t:  (kg_batch_size)
        """
        r_embed = self.relation_embed(r)                 # (kg_batch_size, relation_dim)
        W_r = self.W_R[r]                                # (kg_batch_size, entity_dim, relation_dim)

        h_embed = self.entity_user_embed(h)              # (kg_batch_size, entity_dim)
        pos_t_embed = self.entity_user_embed(pos_t)      # (kg_batch_size, entity_dim)
        neg_t_embed = self.entity_user_embed(neg_t)      # (kg_batch_size, entity_dim)

        r_mul_h = torch.bmm(h_embed.unsqueeze(1), W_r).squeeze(1)             # (kg_batch_size, relation_dim)
        r_mul_pos_t = torch.bmm(pos_t_embed.unsqueeze(1), W_r).squeeze(1)     # (kg_batch_size, relation_dim)
        r_mul_neg_t = torch.bmm(neg_t_embed.unsqueeze(1), W_r).squeeze(1)     # (kg_batch_size, relation_dim)

        pos_score = torch.sum(torch.pow(r_mul_h + r_embed - r_mul_pos_t, 2), dim=1)     # (kg_batch_size)
        neg_score = torch.sum(torch.pow(r_mul_h + r_embed - r_mul_neg_t, 2), dim=1)     # (kg_batch_size)

        kg_loss = (-1.0) * F.logsigmoid(neg_score - pos_score)
        kg_loss = torch.mean(kg_loss)

        l2_loss = _L2_loss_mean(r_mul_h) + _L2_loss_mean(r_embed) + _L2_loss_mean(r_mul_pos_t) + _L2_loss_mean(r_mul_neg_t)
        loss = kg_loss + self.kg_l2loss_lambda * l2_loss
        return loss


    def cf_embedding(self, mode, g, vars_dict=None):
        g = g.local_var()
        ego_embed = self.entity_user_embed(g.ndata['id'])
        all_embed = [ego_embed]

        for i, layer in enumerate(self.aggregator_layers):
            ego_embed = layer(mode, g, ego_embed, vars_dict, i)
            norm_embed = F.normalize(ego_embed, p=2, dim=1)
            all_embed.append(norm_embed)

        all_embed = torch.cat(all_embed, dim=1)         # (n_users + n_entities, cf_concat_dim)
        return all_embed


    def cf_score(self, mode, g, user_ids, item_ids):
        """
        user_ids:   number of users to evaluate   (n_eval_users)
        item_ids:   number of items to evaluate   (n_eval_items)
        """
        all_embed = self.cf_embedding(mode, g)          # (n_users + n_entities, cf_concat_dim)
        user_embed = all_embed[user_ids]                # (n_eval_users, cf_concat_dim)
        item_embed = all_embed[item_ids]                # (n_eval_items, cf_concat_dim)

        cf_score = torch.matmul(user_embed, item_embed.transpose(0, 1))    # (n_eval_users, n_eval_items)
        return cf_score


    def calc_cf_loss(self, mode, g, user_ids, item_pos_ids, item_neg_ids, vars_dict=None, multi=False):
        """
        user_ids:       (cf_batch_size)
        item_pos_ids:   (cf_batch_size)
        item_neg_ids:   (cf_batch_size)
        """
        all_embed = self.cf_embedding(mode, g, vars_dict)                      # (n_users + n_entities, cf_concat_dim)
        user_embed = all_embed[user_ids]                            # (cf_batch_size, cf_concat_dim)
        item_pos_embed = all_embed[item_pos_ids]                    # (cf_batch_size, cf_concat_dim)
        item_neg_embed = all_embed[item_neg_ids]                    # (cf_batch_size, cf_concat_dim)

        if multi:
            pos_score = torch.sum(user_embed * item_pos_embed, dim=1)   # (cf_batch_size)
            neg_score = torch.sum(user_embed * item_neg_embed, dim=1)   # (cf_batch_size)
        else:
            pos_score = torch.sum(user_embed * item_pos_embed)  # (cf_batch_size)
            neg_score = torch.sum(user_embed * item_neg_embed)  # (cf_batch_size)

        cf_loss = (-1.0) * F.logsigmoid(pos_score - neg_score)
        cf_loss = torch.mean(cf_loss)

        l2_loss = _L2_loss_mean(user_embed, multi) + _L2_loss_mean(item_pos_embed, multi) + _L2_loss_mean(item_neg_embed, multi)
        loss = cf_loss + self.cf_l2loss_lambda * l2_loss
        return loss

    def meta_cf(self, mode, train_g, support_u, support_pos_i, support_neg_i, query_u, query_pos_i, query_neg_i):
        init_weight = self.copy_agg_parameters()
        weight_names = list(init_weight.keys())

        loss = self.calc_cf_loss(mode, train_g, support_u, support_pos_i, support_neg_i)
        grad = torch.autograd.grad(loss, init_weight.values())

        fast_weight = {}
        for idx, weight_name in enumerate(weight_names):
            fast_weight[weight_name] = init_weight[weight_name] - self.local_lr * grad[idx]
     
        for i in range(2):
            loss = self.calc_cf_loss(mode, train_g, support_u, support_pos_i, support_neg_i, fast_weight)
            grad = torch.autograd.grad(loss, fast_weight.values())
        
            for idx, weight_name in enumerate(weight_names):
                fast_weight[weight_name] = fast_weight[weight_name] - self.local_lr * grad[idx]

        loss = self.calc_cf_loss(mode, train_g, query_u, query_pos_i, query_neg_i, fast_weight)

        return loss

    def forward(self, mode, *input):
        if mode == 'calc_att':
            return self.compute_attention(*input)
        if mode == 'meta_cf':
            return self.meta_cf(mode, *input)
        if mode == 'calc_cf_loss':
            return self.calc_cf_loss(mode, *input)
        if mode == 'calc_kg_loss':
            return self.calc_kg_loss(*input)
        if mode == 'predict':
            return self.cf_score(mode, *input)


