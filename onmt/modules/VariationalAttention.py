from collections import namedtuple

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from onmt.Utils import aeq, sequence_mask, Params, DistInfo


class VariationalAttention(nn.Module):
    """
    Global attention takes a matrix and a query vector. It
    then computes a parameterized convex combination of the matrix
    based on the input query.

    Constructs a unit mapping a query `q` of size `dim`
    and a source matrix `H` of size `n x dim`, to an output
    of size `dim`.


    .. mermaid::

       graph BT
          A[Query]
          subgraph RNN
            C[H 1]
            D[H 2]
            E[H N]
          end
          F[Attn]
          G[Output]
          A --> F
          C --> F
          D --> F
          E --> F
          C -.-> G
          D -.-> G
          E -.-> G
          F --> G

    All models compute the output as
    :math:`c = \sum_{j=1}^{SeqLength} a_j H_j` where
    :math:`a_j` is the softmax of a score function.
    Then then apply a projection layer to [q, c].

    However they
    differ on how they compute the attention score.

    * Luong Attention (dot, general):
       * dot: :math:`score(H_j,q) = H_j^T q`
       * general: :math:`score(H_j, q) = H_j^T W_a q`


    * Bahdanau Attention (mlp):
       * :math:`score(H_j, q) = v_a^T tanh(W_a q + U_a h_j)`


    Args:
       dim (int): dimensionality of query and key
       coverage (bool): use coverage term
       attn_type (str): type of attention to use, options [dot,general,mlp]

    """
    def __init__(
        self, dim,
        p_dist_type="dirichlet",
        q_dist_type="dirichlet",
        e_dist_type="dirichlet",
        use_prior=False,
        scoresF=F.softplus,
        n_samples=1,
        mode="sample",
        input_feed_type="mean",
    ):
        super(VariationalAttention, self).__init__()

        # attn type is always general
        # no coverage crap
        self.dim = dim
        self.p_dist_type = p_dist_type
        self.q_dist_tyqe = q_dist_type
        self.use_prior = use_prior
        self.scoresF = scoresF
        self.n_samples = n_samples
        self.mode = mode

        self.linear_in = nn.Linear(dim, dim, bias=False)

        if self.p_dist_type == "log_normal":
            self.linear_1 = nn.Linear(dim + dim, 100)
            self.linear_2 = nn.Linear(100, 100)
            self.softplus = torch.nn.Softplus()
            self.mean_out = nn.Linear(100, 1)
            self.var_out = nn.Linear(100, 1)
        # mlp wants it with bias
        out_bias = False
        self.linear_out = nn.Linear(dim*2, dim, bias=out_bias)

        self.sm = nn.Softmax(dim=-1)
        self.tanh = nn.Tanh()

    def score(self, h_t, h_s):
        """
        Args:
          h_t (`FloatTensor`): sequence of queries `[batch x tgt_len x dim]`
          h_s (`FloatTensor`): sequence of sources `[batch x src_len x dim]`

        Returns:
          :obj:`FloatTensor`:
           raw attention scores (unnormalized) for each src index
          `[batch x tgt_len x src_len]`

        """

        # Check input sizes
        src_batch, src_len, src_dim = h_s.size()
        tgt_batch, tgt_len, tgt_dim = h_t.size()
        aeq(src_batch, tgt_batch)
        aeq(src_dim, tgt_dim)
        aeq(self.dim, src_dim)

        h_t_ = h_t.view(tgt_batch*tgt_len, tgt_dim)
        h_t_ = self.linear_in(h_t_)
        h_t = h_t_.view(tgt_batch, tgt_len, tgt_dim)
        h_s_ = h_s.transpose(1, 2)
        # (batch, t_len, d) x (batch, d, s_len) --> (batch, t_len, s_len)
        return torch.bmm(h_t, h_s_)

    def get_raw_scores(self, h_t, h_s):
        """
            For log normal.
        """
        src_batch, src_len, src_dim = h_s.size()
        tgt_batch, tgt_len, tgt_dim = h_t.size()
        aeq(src_batch, tgt_batch)
        aeq(src_dim, tgt_dim)
        aeq(self.dim, src_dim)
        
        h_t_expand = h_t.unsqueeze(2).expand(-1, -1, src_len, -1)
        h_s_expand = h_s.unsqueeze(1).expand(-1, tgt_len, -1, -1)
        # [batch, tgt_len, src_len, src_dim]
        h_expand = torch.cat((h_t_expand, h_s_expand), dim=3)
        h_fold = h_expand.contiguous().view(-1, src_dim + tgt_dim)
        
        h_enc = self.softplus(self.linear_1(h_fold))
        h_enc = self.softplus(self.linear_2(h_enc))
        
        h_mean = self.softplus(self.mean_out(h_enc))
        h_var = self.softplus(self.var_out(h_enc))
        
        h_mean = h_mean.view(tgt_batch, tgt_len, src_len)
        h_var = h_var.view(tgt_batch, tgt_len, src_len)
        return [h_mean, h_var]

    def sample_attn(self, params, n_samples=1, lengths=None, mask=None):
        dist_type = params.dist_type
        if dist_type == "dirichlet":
            alpha = params.alpha
            K = n_samples
            N = alpha.size(0)
            T = alpha.size(1)
            S = alpha.size(2)
            SAD=False
            if not SAD:
                attns = torch.distributions.Dirichlet(
                   params.alpha.cpu().view(N*T, S)
                ).rsample(
                    torch.Size([n_samples])
                ).view(K, N, T, S)
            else:
                # alphas: N x T x S
                alphas = params.alpha.cpu()
                K = n_samples
                N = alphas.size(0)
                T = alphas.size(1)
                S = alphas.size(2)
                samples = []
                for alpha, length in zip(alphas.split(1, dim=0), lengths.tolist()):
                    # sample: K x 1 x T x S
                    sample = torch.distributions.Dirichlet(alpha[:,:,:length]) \
                        .rsample(torch.Size([n_samples]))
                    s = sample.size(-1)
                    if s < S:
                        sample = torch.cat([sample, torch.zeros(K, 1, T, S-s)], dim=-1)
                    samples.append(sample)
                #lol = torch.zeros(K, N, T, S)
                attns = torch.cat(samples, dim=1)
                # try to not include boundaries here
            attns = attns.to(params.alpha)
            # fill in zeros?
            attns.data.masked_fill_(1-mask.unsqueeze(0), 0)
        elif dist_type == "categorical":
            alpha = params.alpha
            log_alpha = params.log_alpha
            K = n_samples
            N = alpha.size(0)
            T = alpha.size(1)
            S = alpha.size(2)
            attns_id = torch.distributions.categorical.Categorical(
               params.alpha.cpu().view(N*T, S)
            ).sample(
                torch.Size([n_samples])
            ).view(K, N, T, 1)
            attns = torch.Tensor(K, N, T, S).zero_()
            attns.scatter_(3, attns_id, 1)
            attns = attns.to(params.alpha)
            # fill in zeros?
            #attns.data.masked_fill_(1-mask.unsqueeze(0), 0)
            # log alpha: K, N, T, S
            log_alpha = log_alpha.unsqueeze(0).expand(K, N, T, S)
            sample_log_probs = log_alpha.gather(3, attns_id.to(log_alpha.device)).squeeze(3)
            return attns, sample_log_probs
        elif dist_type == "none":
            pass
        else:
            raise Exception("Unsupported dist")
        return attns

    def forward(self, input, memory_bank, memory_lengths=None, coverage=None, q_scores=None):
        """

        Args:
          input (`FloatTensor`): query vectors `[batch x tgt_len x dim]`
          memory_bank (`FloatTensor`): source vectors `[batch x src_len x dim]`
          memory_lengths (`LongTensor`): the source context lengths `[batch]`
          coverage (`FloatTensor`): None (not supported yet)

        Returns:
          (`FloatTensor`, `FloatTensor`):

          * Weighted context vector `[tgt_len x batch x dim]`
          * Attention distribtutions for each query
             `[tgt_len x batch x src_len]`
          * Unormalized attention scores for each query 
            `[batch x tgt_len x src_len]`
        """

        # one step input
        if input.dim() == 2:
            one_step = True
            input = input.unsqueeze(1)
            if q_scores is not None:
                # oh, I guess this is super messy
                if q_scores.alpha is not None:
                    q_scores = Params(
                        alpha=q_scores.alpha.unsqueeze(1),
                        log_alpha=q_scores.log_alpha.unsqueeze(1),
                        dist_type=q_scores.dist_type,
                    )
        else:
            one_step = False

        batch, sourceL, dim = memory_bank.size()
        batch_, targetL, dim_ = input.size()
        aeq(batch, batch_)
        aeq(dim, dim_)
        aeq(self.dim, dim)

        # compute attention scores, as in Luong et al.
        #align = self.score(input, memory_bank)
        assert (self.p_dist_type == 'categorical')
        # Softmax to normalize attention weights
        # Params should be T x N x S
        if self.p_dist_type == "dirichlet":
            log_scores = self.score(input, memory_bank)
            #print("log alpha min: {}, max: {}".format(log_scores.min(), log_scores.max()))
            scores = self.scoresF(log_scores)
            if memory_lengths is not None:
                # mask : N x T x S
                mask = sequence_mask(memory_lengths)
                mask = mask.unsqueeze(1)  # Make it broadcastable.
                log_scores.data.masked_fill_(1 - mask, float("-inf"))
                scores.data.masked_fill_(1-mask, math.exp(-10))

            c_align_vectors = F.softmax(log_scores, dim=-1)

            # change scores to params
            p_scores = Params(
                alpha=scores,
                dist_type=self.p_dist_type,
            )
        elif self.p_dist_type == "log_normal":
            raise Exception("Buggy")
            p_scores = self.get_raw_scores(input, memory_bank)
        elif self.p_dist_type == "categorical":
            scores = self.score(input, memory_bank)
            if memory_lengths is not None:
                # mask : N x T x S
                mask = sequence_mask(memory_lengths)
                mask = mask.unsqueeze(1)  # Make it broadcastable.
                scores.data.masked_fill_(1 - mask, -float('inf'))
            scores = self.sm(scores)

            c_align_vectors = scores

            p_scores = Params(
                alpha=scores,
                dist_type=self.p_dist_type,
            )

        # each context vector c_t is the weighted average
        # over all the source hidden states
        context_c = torch.bmm(c_align_vectors, memory_bank)
        concat_c = torch.cat([context_c, input], -1)
        # N x T x H
        h_c = self.tanh(self.linear_out(concat_c))

        # sample or enumerate
        # It's possible that I will actually want these samples...
        # If I need them, I need to pass them into dist_info.
        # y_align_vectors: K x N x T x S
        q_sample, p_sample, sample_log_probs = None, None, None
        if self.mode == "sample":
            if q_scores is None or self.use_prior:
                assert (False)
                p_sample = self.sample_attn(
                    p_scores, n_samples=self.n_samples,
                    lengths=memory_lengths, mask=mask if memory_lengths is not None else None)
                y_align_vectors = p_sample
            else:
                if q_scores.dist_type == 'categorical':
                    q_sample, sample_log_probs = self.sample_attn(
                        q_scores, n_samples=self.n_samples,
                        lengths=memory_lengths, mask=mask if memory_lengths is not None else None)
                else:
                    q_sample = self.sample_attn(
                        q_scores, n_samples=self.n_samples, mode=self.mode,
                        lengths=memory_lengths, mask=mask if memory_lengths is not None else None)
                y_align_vectors = q_sample
        elif self.mode == "enum":
            y_align_vectors = None
        """
        # Data should not be K x N x T x S
        if y_align_vectors.dim() == 3:
            # unsqueeze T dim if just y_align_vectors: K x N x S
            y_align_vectors = y_align_vectors.unsqueeze(2)
        """
        #y_align_vectors = c_align_vectors.unsqueeze(0) # sanity check
        # context_y: K x N x T x H
        if y_align_vectors is not None:
            context_y = torch.bmm(
                y_align_vectors.view(-1, targetL, sourceL),
                memory_bank.unsqueeze(0).repeat(self.n_samples, 1, 1, 1).view(-1, sourceL, dim)
            ).view(self.n_samples, batch, targetL, dim)
        else:
            # For enumerate, K = S.
            # memory_bank: N x S x H
            context_y = (memory_bank
                .unsqueeze(0)
                .repeat(targetL, 1, 1, 1)
                .permute(2, 1, 0, 3))
        input = input.unsqueeze(0).expand_as(context_y)
        concat_y = torch.cat([context_y, input], -1)
        # K x N x T x H
        h_y = self.tanh(self.linear_out(concat_y))

        if one_step:
            # N x H
            h_c = h_c.squeeze(1)
            # N x S
            c_align_vectors = c_align_vectors.squeeze(1)

            # K x N x H
            h_y = h_y.squeeze(2)
            # K x N x S
            #y_align_vectors = y_align_vectors.squeeze(2)

            q_scores = Params(
                alpha = q_scores.alpha.squeeze(1) if q_scores.alpha is not None else None,
                dist_type = q_scores.dist_type,
                samples = q_sample.squeeze(2) if q_sample is not None else None,
                sample_log_probs = sample_log_probs.squeeze(2) if sample_log_probs is not None else None,
            ) if q_scores is not None else None
            p_scores = Params(
                alpha = p_scores.alpha.squeeze(1),
                dist_type = p_scores.dist_type,
                samples = p_sample.squeeze(2) if p_sample is not None else None,
            )

            # Check output sizes
            batch_, dim_ = h_c.size()
            aeq(batch, batch_)
            aeq(dim, dim_)
            batch_, sourceL_ = c_align_vectors.size()
            aeq(batch, batch_)
            aeq(sourceL, sourceL_)
        else:
            assert (False)
            # T x N x H
            h_c = h_c.transpose(0, 1).contiguous()
            # T x N x S
            c_align_vectors = c_align_vectors.transpose(0, 1).contiguous()

            # T x K x N x H
            h_y = h_y.permute(2, 0, 1, 3).contiguous()
            # T x K x N x S
            #y_align_vectors = y_align_vectors.permute(2, 0, 1, 3).contiguous()

            q_scores = Params(
                alpha = q_scores.alpha.transpose(0, 1).contiguous(),
                dist_type = q_scores.dist_type,
                samples = q_sample.permute(2, 0, 1, 3).contiguous(),
            )
            p_scores = Params(
                alpha = p_scores.alpha.transpose(0, 1).contiguous(),
                dist_type = p_scores.dist_type,
                samples = p_sample.permute(2, 0, 1, 3).contiguous(),
            )

            # Check output sizes
            targetL_, batch_, dim_ = h_c.size()
            aeq(targetL, targetL_)
            aeq(batch, batch_)
            aeq(dim, dim_)
            targetL_, batch_, sourceL_ = c_align_vectors.size()
            aeq(targetL, targetL_)
            aeq(batch, batch_)
            aeq(sourceL, sourceL_)

        # For now, don't include samples.
        dist_info = DistInfo(
            q = q_scores,
            p = p_scores,
        )

        # h_y: samples from simplex
        #   either K x N x H, or T x K x N x H
        # h_c: convex combination of memory_bank for input feeding
        #   either N x H, or T x N x H
        # align_vectors: convex coefficients / boltzmann dist
        #   either N x S, or T x N x S
        # raw_scores: unnormalized scores
        #   either N x S, or T x N x S
        return h_y, h_c, c_align_vectors, dist_info
