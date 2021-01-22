import torch
from torch.utils.data import Dataset, IterableDataset, DataLoader
import scipy.io as sci
import scipy as sp
import random
import numpy as np
import pdb
from sklearn.cluster import KMeans
import math

class RecData(object):
    def __init__(self, file_name):
        self.file_name = file_name

    def get_data(self,ratio):
        mat = self.load_file(file_name=self.file_name)
        train_mat, test_mat = self.split_matrix(mat, ratio)
        return train_mat, test_mat
    
    def load_file(self,file_name=''):
        if file_name.endswith('.mat'):
            return sci.loadmat(file_name)['data']
        else:
            raise ValueError('not supported file type')

    def split_matrix(self, mat, ratio=0.8):
        mat = mat.tocsr()  #按行读取，即每一行为一个用户
        m,n = mat.shape
        train_data_indices = []
        train_indptr = [0] * (m+1)
        test_data_indices = []
        test_indptr = [0] * (m+1)
        for i in range(m):
            row = [(mat.indices[j], mat.data[j]) for j in range(mat.indptr[i], mat.indptr[i+1])]
            train_idx = random.sample(range(len(row)), round(ratio * len(row)))
            train_binary_idx = np.full(len(row), False)
            train_binary_idx[train_idx] = True
            test_idx = (~train_binary_idx).nonzero()[0]
            for idx in train_idx:
                train_data_indices.append(row[idx]) 
            train_indptr[i+1] = len(train_data_indices)
            for idx in test_idx:
                test_data_indices.append(row[idx])
            test_indptr[i+1] = len(test_data_indices)

        [train_indices, train_data] = zip(*train_data_indices)
        [test_indices, test_data] = zip(*test_data_indices)

        train_mat = sp.sparse.csr_matrix((train_data, train_indices, train_indptr), (m,n))
        test_mat = sp.sparse.csr_matrix((test_data, test_indices, test_indptr), (m,n))
        return train_mat, test_mat




class Sampled_Iterator(IterableDataset):
    def __init__(self, mat, user_embs, item_embs, num_subspace, cluster_dim, num_cluster, num_neg):
        super(Sampled_Iterator, self).__init__()

        self.mat = mat.tocsr()
        self.num_users, self.num_items = mat.shape

        self.center_scores = {}
        self.combine_cluster_idx = torch.zeros((self.num_items))
        self.num_cluster = num_cluster
        self.num_subspace = num_subspace
        self.num_neg = num_neg
        _, latent_dim = user_embs.shape
        assert (latent_dim - num_subspace * cluster_dim) > 0

        for i in range(self.num_subspace):
            start_idx = i * cluster_dim
            end_idx = (i+1) * cluster_dim
            cluster_kmeans = KMeans(n_clusters=self.num_cluster, random_state=0).fit(item_embs[:,start_idx:end_idx].detach().numpy())
            centers = cluster_kmeans.cluster_centers_
            codes = cluster_kmeans.labels_

            self.center_scores[i] = torch.matmul(user_embs[:,start_idx:end_idx] , torch.tensor(centers).T)
            self.combine_cluster_idx = self.combine_cluster_idx * self.num_cluster + codes
        
        self.delta_ruis = torch.matmul(user_embs[:,end_idx:], item_embs[:,end_idx:].T)
    
    def __iter__(self):
        return self.negative_sampler(self.num_neg)()

    def preprocess(self, user_id):
        import time
        self.exist = set(self.mat.indices[j] for j in range(self.mat.indptr[user_id], self.mat.indptr[user_id + 1]))

        self.user_id = user_id
        delta_ruis_u = torch.exp(self.delta_ruis[user_id]).detach().numpy()
        combine_mat = sp.sparse.csr_matrix((delta_ruis_u, (np.arange(self.num_items), self.combine_cluster_idx)), shape=(self.num_items, self.num_cluster**self.num_subspace))

        code_mat = combine_mat.tocsc()
        self.items_in_combine_cluster = [np.array([code_mat.indices[j] for j in range(code_mat.indptr[c], code_mat.indptr[c+1])]) for c in range(self.num_cluster**self.num_subspace)]
        # w_kk : \sum_{j\in K K'} exp(rui)
        self.w_kk = np.squeeze(combine_mat.sum(axis=0).A)


        shape_list = [self.num_cluster for x in range(self.num_subspace)]
        kk_mtx = self.w_kk.reshape(shape_list)
        self.sample_prob = {}
        self.sample_prob[self.num_subspace-1] = torch.tensor(kk_mtx)
        for i in range(self.num_subspace-2, -1, -1):
            r_centers = torch.exp(self.center_scores[i][user_id]).unsqueeze(-1).detach().numpy()
            kk_mtx = np.matmul(kk_mtx, r_centers).squeeze(-1)
            self.sample_prob[i] = torch.tensor(kk_mtx)


    def __sampler__(self, user_id, pos_id):
        def sample():
            idx = []
            probs = 1.0
            for i in range(self.num_subspace):
                sample_probs = self.sample_prob[i]
                if len(idx) > 0:
                    for history_cluster in idx:
                        sample_probs = sample_probs[history_cluster]
                extra = sample_probs.squeeze()
                total_score = self.center_scores[i][self.user_id] + torch.log(extra)
                idx_cluster, estimate_par = self.sample_from_gumbel_noise(total_score)
                idx.append(idx_cluster)
                probs *= total_score[idx_cluster] / torch.exp(torch.negative(estimate_par))
            
            # sample from the final items
            index_combine_cluster = 0
            for ii in idx:
                index_combine_cluster = index_combine_cluster * self.num_cluster + ii
            items_index = self.items_in_combine_cluster[index_combine_cluster]
            rui_items = self.delta_ruis[self.user_id][items_index]
            item_sample_index, estimate_par = self.sample_from_gumbel_noise(rui_items)
            probs *= rui_items[item_sample_index] / torch.exp(torch.negative(estimate_par))
            return items_index[item_sample_index], probs
        return sample, self.exist

    def negative_sampler(self, neg):
        def sample_negative(user_id, pos_id):
            sample, exist_ = self.__sampler__(user_id, pos_id)
            k, p = sample()
            while k in exist_:
                k, p = sample()
            return k, p

        def generate_tuples():
            for i in np.random.permutation(self.num_users):
                self.preprocess(i)
                # print('user id : ', i, ', num_rated_item : ', len(self.exist))
                for j in self.exist:
                    neg_item = [0] * neg
                    prob = [0.] * neg
                    for o in range(neg):
                        neg_item[o], prob[o] = sample_negative(i, j)
                    # yield ([i], [j], neg_item, prob), 1
                    yield torch.LongTensor([i]), torch.LongTensor([j]), torch.LongTensor(neg_item), torch.Tensor(prob)
        return generate_tuples
    
    def sample_gumbel_noise(self, inputs,eps=1e-7):
        u = torch.rand(inputs.shape)
        return -torch.log(eps - torch.log(u + eps))
    
    def sample_from_gumbel_noise(self, scores):
        return torch.argmax(scores  + self.sample_gumbel_noise(scores)), torch.max(scores + self.sample_gumbel_noise(scores))


class Fast2_Sampler_Loader(Sampled_Iterator):
    def __init__(self, mat, user_embs, item_embs, num_subspace, cluster_dim, num_cluster, num_neg):
        super(Fast2_Sampler_Loader, self).__init__(mat, user_embs, item_embs, num_subspace, cluster_dim, num_cluster, num_neg)
        self.start_user  = 0
        self.end_user = self.num_users

    def __iter__(self):
        return self.negative_sampler(self.num_neg, self.start_user, self.end_user)()
    
    def __sampler__(self, user_id, pos_id):
        def sample():
            idx = []
            probs = []
            start_flag = True
            for i in range(self.num_subspace):
                sample_probs = self.sample_prob[i]
                if len(idx) > 0:
                    start_flag = False
                    for history_cluster in idx:
                        sample_probs = sample_probs[history_cluster]
                extra = sample_probs.squeeze()
                total_score = self.center_scores[i][self.user_id] + torch.log(extra)
                idx_clusters, estimate_par = self.sample_from_gumbel_noise(total_score, start_flag)
                idx.append(idx_clusters)
                if start_flag:
                    new_prob =  torch.exp(total_score[idx_clusters]) * torch.exp(torch.negative(estimate_par.values))
                else:
                    row_idx = torch.LongTensor([x for x in range(self.num_neg)])
                    new_prob = torch.exp(total_score[row_idx,idx_clusters]) * torch.exp(torch.negative(estimate_par.values))
                probs.append(new_prob)
            
            fprobs = torch.mul(probs[0], probs[1])
            # sample from the final items
            
            items = []
            final_probs = []
            i = 0
            while True:
                index_combine_cluster = idx[0][i] * self.num_cluster + idx[1][i]
                items_index = self.items_in_combine_cluster[index_combine_cluster]
                if len(items_index) == 1:
                    items.append(items_index[0])
                    final_probs.append(fprobs[i])
                elif len(items_index) < 1:
                    continue
                else:
                    rui_items = self.delta_ruis[self.user_id][items_index]
                    item_sample_index, estimate_par = self.sample_from_gumbel_noise(rui_items)
                    delta_probs = torch.exp(rui_items[item_sample_index]) * torch.exp(torch.negative(estimate_par.values))
                    items.append(items_index[item_sample_index])
                    final_probs.append(fprobs[i] * delta_probs)
                
                i += 1
                
                # print(i, self.num_neg-1)
                if i > (self.num_neg-1):
                    break
            return items, final_probs
        return sample

    def negative_sampler(self, neg, start_id, end_id):
        def sample_negative(user_id, pos_id):
            sample = self.__sampler__(user_id, pos_id)
            k, p = sample()
            return k, p

        def generate_tuples():
            for i in np.random.permutation(range(start_id, end_id)):
                self.preprocess(i)
                # print('user id : ', i, ', num_rated_item : ', len(self.exist))
                for j in self.exist:
                    # print('num_samples : ', self.num_neg)
                    neg_item, probs = sample_negative(i, j)
                    yield torch.LongTensor([i]), torch.LongTensor([j]), torch.LongTensor(neg_item), torch.Tensor(probs)
        return generate_tuples

    def sample_gumbel_noise(self, inputs,eps=1e-7, start_flag=False):
        if start_flag:
            us = torch.rand(inputs.unsqueeze(-1).expand(-1, self.num_neg).shape)
        else:
            us = torch.rand(inputs.shape)
        return -torch.log(- torch.log(us + eps) + eps)
    
    def sample_from_gumbel_noise(self, scores, start_flag=False):
        if start_flag:
            tmp = scores.unsqueeze(-1) + self.sample_gumbel_noise(scores, start_flag=True)
            return torch.argmax(tmp, dim=0), torch.max(tmp, dim=0)
        else:
            tmp = scores + self.sample_gumbel_noise(scores)
            return torch.argmax(tmp, dim=-1), torch.max(tmp, dim=-1)
        


def worker_init_fn(worker_id):
     worker_info = torch.utils.data.get_worker_info()
     dataset = worker_info.dataset  # the dataset copy in this worker process
     overall_start = dataset.start_user
     overall_end = dataset.end_user
     # configure the dataset to only process the split workload
     per_worker = int(math.ceil((overall_end - overall_start) / float(worker_info.num_workers)))
     worker_id = worker_info.id
     dataset.start_user = overall_start + worker_id * per_worker
     dataset.end_user = min(dataset.start_user + per_worker, overall_end)

if __name__ == "__main__":
    data = RecData('ml100kdata.mat')
    train, test = data.get_data(0.8)
    print(train.shape, test.shape)
    user_num, item_num = train.shape
    user_emb, item_emb = torch.rand((user_num, 20)), torch.rand((item_num, 20))

    # test_iter = Sampled_Iterator(train[:500], user_emb, item_emb, 2, 6, 32, 5)
    test_iter = Fast_Sampler_Loader(train[:500], user_emb, item_emb, 2, 6, 32, 5)
    test_dataloader = DataLoader(test_iter, batch_size=1024, num_workers=8, worker_init_fn=worker_init_fn)
    import time
    tmp = time.time()
    t0 = tmp
    data_len = 0
    for idx, x in enumerate(test_dataloader):
        data_len += len(x[0])
        # import pdb; pdb.set_trace()
        if (idx % 5) < 1:
            tt = time.time()
            print(idx, tt-tmp)
            tmp = tt
        print(idx)
    print(time.time() - t0)
    print(train[:500].nnz, data_len)