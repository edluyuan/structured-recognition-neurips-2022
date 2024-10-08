import torch
import torch.nn as nn
from torch.distributions import Normal, MultivariateNormal, kl_divergence
import numpy as np
from typing import Union
import copy

from gpvae.utils.matrix import add_diagonal
from gpvae.likelihoods.base import Likelihood
from gpvae.kernels.kernels import Kernel
from gpvae.kernels.composition import KernelList

JITTER = 1e-5

class VAE(nn.Module):
    def __init__(self, recog_model: Likelihood, gen_model: Likelihood, latent_dim: int, device=torch.device('cpu')):
        super().__init__()
        
        self.recog_model = recog_model
        self.gen_model = gen_model
        self.latent_dim = latent_dim
        self.device = device
        
    def pf(self):
        mu = torch.zeros((self.latent_dim, ), device=self.device)
        sigma = torch.ones((self.latent_dim, ), device=self.device)
        return Normal(mu, sigma)
    
    def qf(self, y):
        return self.recog_model(y)
    
    def free_energy(self, y, num_samples=1):
        batch_size = y.shape[0]
        pf = self.pf()
        qf = self.qf(y)
        
        kl = kl_divergence(pf, qf).sum()
        
        f_samples = qf.rsample((num_samples, ))
        log_py_f = 0.0
        
        for f in f_samples:
            log_py_f += self.gen_model.log_likelihood(f, y).sum()
        
        log_py_f = log_py_f / num_samples
        
        free_energy = log_py_f - kl
        return free_energy / batch_size
    
    @torch.no_grad()
    def generation(self, y=None, num_samples=1):
        if y is None:
            qf = self.pf()
        else:
            qf = self.qf(y)
        
        f_samples = qf.sample((num_samples, ))
        gen_mus, gen_samples = [], []
        
        for f in f_samples:
            py_f = self.gen_model(f)
            gen_mus.append(py_f.mean)
            gen_samples.append(py_f.sample())
        
        gen_mus = torch.stack(gen_mus).mean(dim=0)
        gen_sigmas = torch.stack(gen_samples).std(dim=0)
        
        return gen_mus, gen_sigmas, gen_samples
        

class GPVAE(VAE):
    def __init__(self, recog_model: Likelihood, gen_model: Likelihood, latent_dim: int, kernel: Union[Kernel, list], 
                 add_jitter: bool=False, device=torch.device('cpu')):
        super().__init__(recog_model=recog_model, gen_model=gen_model, latent_dim=latent_dim, device=device)
        self.add_jitter = add_jitter
        
        if not isinstance(kernel, list):
            kernels = [copy.deepcopy(kernel) for _ in range(latent_dim)]
            self.kernels = KernelList(kernels)
        else:
            assert len(kernel) == latent_dim, 'Number of kernels must match the latent dimension'
            self.kernels = KernelList(kernels)
    
    def pf(self, x, diag=False):
        batch_size = x.shape[0]
        mu = torch.zeros((self.latent_dim, batch_size), device=self.device)
        cov = self.kernels.forward(x, x, diag=diag) + self.add_jitter * JITTER * torch.eye(batch_size, device=self.device).unsqueeze(0)
        regularization = 1e-6 * torch.eye(cov.shape[-1]).to(cov.device)
        cov_regularized = cov + regularization
        # add jittering might break when self.latent_dim == 1
        cov_chol = torch.linalg.cholesky(cov_regularized)
        #cov_chol = torch.linalg.cholesky(cov)
        #cov_chol = torch.cholesky(cov)
        
        pf = MultivariateNormal(loc=mu, scale_tril=cov_chol)
        return pf
    
    def recog_factor(self, y):
        return self.recog_model(y)

    def qf(self, x=None, y=None, pf=None, rf=None, diag=False, x_test=None):
        if pf is None:
            assert x is not None
            pf = self.pf(x, diag)
        
        pf_cov, pf_chol = pf.covariance_matrix, pf.scale_tril
        
        if rf is None:
            assert y is not None
            rf = self.recog_factor(y)
        
        rf_mu = rf.mean.transpose(0, 1) # (B, K) -> (K, B)
        rf_std = rf.stddev.transpose(0, 1)
        rf_cov = torch.square(rf_std).diag_embed() # (K, B) -> (K, B, B)
        
        a = torch.linalg.solve_triangular(pf_chol, rf_cov.pow(0.5), upper=False)
        a = a.matmul(a.transpose(-1, -2))
        a = add_diagonal(a, 1, device=self.device)
        a_chol = torch.linalg.cholesky(a)
        
        b1 = torch.linalg.solve_triangular(pf_chol, rf_mu.unsqueeze(2), upper=False)
        b = torch.linalg.solve_triangular(a_chol, b1, upper=False)
        
        c = torch.linalg.solve_triangular(a_chol, pf_chol.transpose(-1, -2), upper=False)
        
        # the seemingly contrived computations are for numerical stability
        # essentially we are computing m = S^{-1}\Sigma^{-1}\mu, S^{-1} = K^{-1} + \Sigma^{-1}
        qf_mu = c.transpose(-1, -2).matmul(b).squeeze(2)
        qf_cov = pf_cov - c.transpose(-1, -2).matmul(c)
        qf = MultivariateNormal(qf_mu, qf_cov)
        
        qs = None
        if x_test is not None:
            ps = self.pf(x_test)
            ps_cov = ps.covariance_matrix
            
            ksf = self.kernels.forward(x_test, x)
            kfs = ksf.transpose(-1, -2)
            
            b1 = torch.linalg.solve_triangular(pf_chol, qf_mu.unsqueeze(2), upper=False)
            
            e = torch.linalg.solve_triangular(pf_chol, kfs)
            f = pf_cov - qf_cov
            f_chol = torch.cholesky(f)
            g = torch.linalg.solve_triangular(pf_chol, f_chol)
            h = g.transpose(-1, -2).matmul(e)
            qs_cov = ps_cov - h.transpose(-1, -2).matmul(h)
            qs_mu = torch.linalg.solve_triangular(pf_chol, kfs).transpose(-1, -2).matmul(b1).squeeze(2)
            qs = MultivariateNormal(qs_mu, qs_cov)
            
        return qf, qs
    
    def free_energy(self, x, y, num_samples=1):
        batch_size = y.shape[0]
        pf = self.pf(x)
        rf = self.recog_factor(y)
        qf, _ = self.qf(pf, rf)
        
        kl = kl_divergence(qf, pf).sum()
        
        qf_sigma = torch.stack([cov.diag().pow(0.5) for cov in qf.covariance_matrix])
        qf_marginals = Normal(qf.mean, qf_sigma)
        f_samples = qf_marginals.rsample((num_samples, ))
        
        log_py_f = 0.
        for f in f_samples:
            log_py_f = log_py_f + self.gen_model.log_likelihood(f.T, y).sum()
        
        log_py_f = log_py_f / num_samples
        
        free_energy = log_py_f - kl
        return free_energy / batch_size
    
    @torch.no_grad()
    def generation(self, x, y=None, x_test=None, num_samples=1):
        if y is None:
            if x_test is None:
                qf = self.pf(x)
            else:
                qf = self.pf(x_test)
        else:
            qf = self.qf(x=x, y=y, x_test=x_test)
        
        f_samples = qf.sample((num_samples, ))
        y_mus, y_samples = [], []
        for f in f_samples:
            py_f = self.gen_model.log_likelihood(f.T)
            y_mus.append(py_f.mean)
            y_samples.append(py_f.sample())
        
        y_mus = torch.stack(y_mus).mean(dim=0)
        y_sigmas = torch.stack(y_samples).std(dim=0)
        
        return y_mus, y_sigmas, y_samples
    

class SGPVAE(GPVAE):
    def __init__(self, recog_model: Likelihood, gen_model: Likelihood, latent_dim: int, kernel: Union[list, Kernel], z: torch.Tensor, 
                 add_jitter: bool=False, fixed_inducing: bool=False, device=torch.device('cpu')):
        super().__init__(recog_model=recog_model, gen_model=gen_model, latent_dim=latent_dim, kernel=kernel, add_jitter=add_jitter, 
                         device=device)
        self.z = nn.Parameter(z.to(self.device), requires_grad=not fixed_inducing) # inducing locations
    
    def qf(self, x, y=None, pu=None, rf=None, diag=False, x_test=None, full_cov=False):
        if pu  is None:
            pu = self.pf(self.z)
        
        pu_chol = pu.scale_tril
        
        if rf is None:
            rf = self.rf(y)
        
        rf_mu = rf.mean.transpose(0, 1)
        rf_sigma = rf.stddev.transpose(0, 1)
        rf_precision = rf.pow(-2).diag_embed()
        
        kfu = self.kernels.forward(x, self.z)
        kuf = kfu.transpose(-1, -2)
        
        a = torch.linalg.solve_triangular(pu_chol, kuf, upper=False)
        
        b = a.matmul(rf_precision).matmul(a.transpose(-1, -2))
        b = add_diagonal(b, 1, device=self.device)
        b_chol = torch.linalg.cholesky(b)
        
        c = a.matmul(rf_precision).matmul(rf_mu.unsqueeze(2))
        c = torch.linalg.solve_triangular(b_chol, c, upper=False)
        
        d = torch.linalg.solve_triangular(b_chol.transpose(-1, -2), c, upper=True)
        d = torch.linalg.solve_triangular(pu_chol.transpose(-1, -2), d, upper=True)
        
        pf = self.pf(x, diag=diag)
        pf_cov, pf_chol = pf.covariance_matrix, pf.scale_tril
        
        e = torch.linalg.solve_triangular(b_chol, a, upper=False)
        
        qf_cov = pf_cov - a.transpose(-1, -2).matmul(a) + e.transpose(-1, -2).matmul(e)
        qf_mu = kfu.matmul(d).squeeze(2)
        qf = MultivariateNormal(qf_mu, qf_cov)
        
        g = torch.linalg.solve_triangular(b_chol, pu_chol, upper=False)
        qu_mu = torch.linalg.solve_triangular(b_chol.transpose(-1, -2), c, upper=True)
        qu_mu = pu_chol.matmul(qu_mu).squeeze(2)
        qu_cov = g.transpose(-1, -2).matmul(g)
        qu = MultivariateNormal(qu_mu, qu_cov)
        
        qs = None
        if x_test is not None:
            ps = self.pf(x_test, diag=not full_cov)
            ps_cov = ps.covariance_matrix
            
            ksf = self.kernels.forward(x_test, x)
            kfs = ksf.transpose(-1, -2)
            
            b1 = torch.linalg.solve_triangular(pf_chol, qf_mu.unsqueeze(2), upper=False)
            
            e = torch.linalg.solve_triangular(pf_chol, kfs)
            f = pf_cov - qf_cov
            f_chol = torch.cholesky(f)
            g = torch.linalg.solve_triangular(pf_chol, f_chol)
            h = g.transpose(-1, -2).matmul(e)
            qs_cov = ps_cov - h.transpose(-1, -2).matmul(h)
            qs_mu = torch.linalg.solve_triangular(pf_chol, kfs).transpose(-1, -2).matmul(b1).squeeze(2)
            qs = MultivariateNormal(qs_mu, qs_cov)
        
        return qf, qu, qs
    
    def free_energy(self, x, y, num_samples=1):
        batch_size = y.shape[0]
        pu = self.pf(self.z)
        rf = self.recog_factor(y)
        qf, qu, _ = self.qf(x=x, pu=pu, rf=rf)
        
        kl = kl_divergence(qu, pu).sum()
        
        qf_sigma = torch.stack([cov.diag().pow(0.5) for cov in qf.covariance_matrix])
        qf_marginals = Normal(qf.mean, qf_sigma)
        f_samples = qf_marginals.rsample((num_samples, ))
        log_py_f = 0.
        for f in f_samples:
            log_py_f = log_py_f + self.gen_model.log_likelihood(f.T, y).sum()
        
        log_py_f = log_py_f / num_samples
        free_energy = log_py_f - kl
        
        return free_energy / batch_size

    @torch.no_grad()
    def generation(self, x, y=None, x_test=None, full_cov=True, num_samples=1):
        if y is None:
            if x_test is None:
                qf = self.pf(x)
            else:
                qf = self.pf(x_test)
        else:
            qf = self.qf(x=x, y=y, x_test=x_test, full_cov=full_cov)
        
        if full_cov:
            f_samples = qf.sample((num_samples, ))
        else:
            qf_sigma = torch.stack([cov.diag().pow(0.5) for cov in qf.covariance_matrix])
            qf_marginals = Normal(qf.mean, qf_sigma)
            f_samples = qf_marginals.sample((num_samples, ))
        
        y_mus, y_samples = [], []
        
        for f in f_samples:
            py_f = self.gen_model(f.T)
            y_mus.append(py_f.mean)
            y_samples.append(py_f.sample())
        
        y_mus = torch.stack(y_mus).mean(dim=0)
        y_sigmas = torch.stack(y_samples).std(dim=0)
        
        return y_mus, y_sigmas, y_samples


class SR_nlGPFA(GPVAE):
    def __init__(self, recog_model: Likelihood, gen_model: Likelihood, latent_dim: int, kernel: Union[list, Kernel], 
                 z: torch.Tensor, add_jitter: bool=False, fixed_inducing: bool=False, h_dim: int=20, affine_weight: torch.Tensor=None, 
                 affine_bias: torch.Tensor=None, device=torch.device('cpu'), orthogonal_reg: float=0.0):
        super().__init__(recog_model=recog_model, gen_model=gen_model, latent_dim=latent_dim, kernel=kernel, 
                         add_jitter=add_jitter, device=device)
        
        self.z = nn.Parameter(z.to(self.device), requires_grad=not fixed_inducing)
        self.num_inducing = z.shape[0]
        self.h_dim = h_dim
        self.affine_weight = nn.Parameter(torch.eye(h_dim, device=self.device) if affine_weight is None else affine_weight, 
                                          requires_grad=True)
        self.affine_bias = nn.Parameter(torch.zeros((h_dim,), device=self.device) if affine_bias is None else affine_bias, 
                                        requires_grad=True)
        
        self.orthogonal_reg = orthogonal_reg
    
    def qf(self, x, y=None, pu=None, rh=None, diag=False, x_test=None, full_cov=False):
        batch_size = x.shape[0]
        if pu is None:
            pu = self.pf(self.z)
        
        pu_chol = pu.scale_tril
        pu_chol_M = torch.block_diag(*pu_chol) # (KM, KM)
        pu_precision = pu.precision_matrix
        pu_cov = torch.block_diag(*pu.covariance_matrix)
        
        if rh is None:
            rh = self.recog_factor(y)
        
        rh_mu = rh.mean
        rh_sigma = rh.stddev
        rh_precision = rh_sigma.pow(-2).diag_embed()
        
        kfu = self.kernels.forward(x, self.z)
        
        F_mat = torch.matmul(kfu, pu_precision).transpose(0, 1) # (K, B, M) -> (B, K, M)
        F_mat = torch.cat([torch.block_diag(*F_mat[n]).unsqueeze(0) for n in range(batch_size)], dim=0) # (B, K, KM)
        
        C_mat = self.affine_weight.unsqueeze(0)
        d_vec = self.affine_bias
        
        a = (C_mat.transpose(-1, -2)).matmul(rh_precision).matmul(C_mat)
        
        b = torch.sum((F_mat.transpose(-1, -2)).matmul(a).matmul(F_mat), dim=0) # (KM, KM)
        b = pu_chol_M.matmul(b).matmul(pu_chol_M.transpose(-1, -2))
        b = add_diagonal(b, 1, self.device)
        b_chol = torch.linalg.cholesky(b)
        
        c = torch.sum((F_mat.transpose(-1, -2)).matmul(C_mat.transpose(-1, -2)).matmul(rh_precision).matmul((rh_mu-d_vec).unsqueeze(-1)), dim=0)

        d = torch.linalg.solve_triangular(b_chol, pu_chol_M, upper=False)
        qu_cov = d.transpose(-1, -2).matmul(d) + self.add_jitter * JITTER * torch.eye(self.latent_dim * self.num_inducing, device=self.device)
        qu_mu = torch.matmul(qu_cov.clone(), c).squeeze(-1)
        qu = MultivariateNormal(qu_mu, qu_cov)
        
        pf = self.pf(x, diag)
        pf_cov, pf_chol = pf.covariance_matrix, pf.scale_tril
        
        qf_mu = F_mat.matmul(qu_mu)
        K_n = torch.diagonal(pf_cov, dim1=-2, dim2=-1).transpose(-1, -2).diag_embed()
        qf_cov = K_n + F_mat.matmul(qu_cov - pu_cov).matmul(F_mat.transpose(-1, -2))
        qf = MultivariateNormal(qf_mu, qf_cov)

        qh_mu = C_mat.matmul(F_mat).matmul(qu_mu) + d_vec
        qh_cov = C_mat.matmul(qf_cov).matmul(C_mat.transpose(-1, -2)) + self.add_jitter * JITTER * \
                 torch.eye(self.h_dim, device=self.device).unsqueeze(0).repeat((batch_size, 1, 1))
        qh = MultivariateNormal(qh_mu, qh_cov)
        
        qs = None
        if x_test is not None:
            ps = self.pf(x_test, diag=not full_cov)
            ps_cov = ps.covariance_matrix
            
            ksf = self.kernels.forward(x_test, x)
            kfs = ksf.transpose(-1, -2)
            
            b1 = torch.linalg.solve_triangular(pf_chol, qf_mu, upper=False)
            
            e = torch.linalg.solve_triangular(pf_chol, kfs)
            f = pf_cov - qf_cov
            f_chol = torch.cholesky(f)
            g = torch.linalg.solve_triangular(pf_chol, f_chol)
            h = g.transpose(-1, -2).matmul(e)
            qs_cov = ps_cov - h.transpose(-1, -2).matmul(h)
            qs_mu = torch.linalg.solve_triangular(pf_chol, kfs).transpose(-1, -2).matmul(b1).squeeze(2)
            qs = MultivariateNormal(qs_mu, qs_cov)
        
        return qh, qu, qf, qs
    
    def flatten_pu(self, pu=None):
        if pu is None:
            pu = self.pf(self.z)
        pu_cov = pu.covariance_matrix
        pu_mu = pu.mean
        pu_full_cov = torch.block_diag(*pu_cov)
        pu_full_mu = pu_mu.reshape(-1)
        pu_full = MultivariateNormal(pu_full_mu, pu_full_cov)
        return pu_full
    
    def free_energy(self, x, y, num_samples=1):
        batch_size = y.shape[0]
        pu = self.pf(self.z)
        pu_flatten = self.flatten_pu(pu)
        rh = self.recog_factor(y)
        qh, qu, _, _ = self.qf(x, pu=pu, rh=rh)
        
        kl = kl_divergence(qu, pu_flatten).sum()
        
        qh_marginal = MultivariateNormal(qh.mean, qh.covariance_matrix)
        h_samples = qh_marginal.rsample((num_samples, ))
        log_py_h = 0.
        for h in h_samples:
            log_py_h = log_py_h + self.gen_model.log_likelihood(h, y).sum()
        
        log_py_h = log_py_h / num_samples
        free_energy = (log_py_h - kl) / batch_size
        
        if self.orthogonal_reg:
            free_energy = free_energy + self.orthogonal_reg * torch.sum(torch.square(self.affine_weight.transpose(-1, -2).matmul(self.affine_weight) \
                - torch.eye(self.latent_dim, device=self.device)))
        
        return free_energy
    
    @torch.no_grad()
    def generation(self, x, y=None, x_test=None, full_cov=True, num_samples=1):
        if y is None:
            if x_test is None:
                qf = self.pf(x)
            else:
                qf = self.pf(x_test)
        else:
            qf, _, _, _ = self.qf(x=x, y=y, x_test=x_test, full_cov=full_cov)
        
        if full_cov:
            f_samples = qf.sample((num_samples, ))
        else:
            qf_sigma = torch.stack([cov.diag().pow(0.5) for cov in qf.covariance_matrix])
            qf_marginals = Normal(qf.mean, qf_sigma)
            f_samples = qf_marginals.sample((num_samples, ))
        
        y_mus, y_samples = [], []
        
        for f in f_samples:
            py_f = self.gen_model(f)
            y_mus.append(py_f.mean)
            y_samples.append(py_f.sample())
        
        y_mus = torch.stack(y_mus).mean(dim=0)
        y_sigmas = torch.stack(y_samples).std(dim=0)
        
        return y_mus, y_sigmas, y_samples


class SR_nlHGPFA(SR_nlGPFA):
    def __init__(self, recog_model, gen_model, latent_dim, kernel, z, add_jitter=True, fixed_inducing=False,
                 h_dim=20, affine_weight=None, affine_bias=None, device=None, orthogonal_reg=0.0,
                 K=10, init_lf=0.1, max_lf=0.5, temp_method='none', init_alpha=0.5, init_T_0=2.0):
        super().__init__(recog_model, gen_model, latent_dim, kernel, z, add_jitter, fixed_inducing,
                         h_dim, affine_weight, affine_bias, device, orthogonal_reg)
        self.K = K
        self.init_lf = init_lf
        self.max_lf = max_lf
        self.temp_method = temp_method
        self.init_alpha = init_alpha
        self.init_T_0 = init_T_0
        self._init_hmc_params()

    def _init_hmc_params(self):
        # Initialize leapfrog step size parameters
        init_lf_reparam = np.log(self.init_lf / (self.max_lf - self.init_lf))
        self.lf_reparam = nn.Parameter(torch.tensor(init_lf_reparam, dtype=torch.float64, device=self.device))

        # Initialize temperature parameters
        if self.temp_method == 'free':
            init_alpha = self.init_alpha  # Should be between 0 and 1
            init_alpha_reparam = np.log(init_alpha / (1 - init_alpha))
            self.alphas_reparam = nn.Parameter(
                torch.tensor(init_alpha_reparam, dtype=torch.float64, device=self.device))
        elif self.temp_method == 'fixed':
            init_T_0 = self.init_T_0  # Should be greater than 1.0
            init_T_0_reparam = np.log(init_T_0 - 1)
            self.T_0_reparam = nn.Parameter(torch.tensor(init_T_0_reparam, dtype=torch.float64, device=self.device))
        else:
            # Use different names to avoid conflict
            self.register_buffer('_T_0', torch.tensor(1., dtype=torch.float64, device=self.device))
            self.register_buffer('_alphas', torch.ones(self.K, dtype=torch.float64, device=self.device))

    def free_energy(self, x, y, num_samples=1):
        batch_size = y.shape[0]
        pu = self.pf(self.z)
        pu_flatten = self.flatten_pu(pu)
        rh = self.recog_factor(y)
        qh, qu, _, _ = self.qf(x, pu=pu, rh=rh)

        # KL divergence between q(u) and p(u)
        kl = kl_divergence(qu, pu_flatten).sum()

        # Sample initial h and p
        q_mu = qh.mean  # [batch_size, h_dim]
        q_sigma = qh.stddev  # [batch_size, h_dim]

        h_0 = q_mu + q_sigma * torch.randn_like(q_mu)
        p_0 = torch.sqrt(self.T_0) * torch.randn_like(h_0)

        # Perform Hamiltonian dynamics to get h_K and p_K
        h_K, p_K = self._his(h_0, p_0, y)

        # Compute expected log-likelihood
        expected_log_likelihood = self.gen_model.log_likelihood(h_K, y).sum(dim=1)  # [batch_size]

        # Compute negative KL term
        log_prob_hK = -0.5 * (h_K ** 2).sum(dim=1)  # Prior over h_K
        log_prob_pK = -0.5 * (p_K ** 2).sum(dim=1)
        sum_log_sigma = q_sigma.log().sum(dim=1)

        log_prob_h0 = -0.5 * (((h_0 - q_mu) / q_sigma) ** 2).sum(dim=1)
        log_prob_p0 = -0.5 / self.T_0 * (p_0 ** 2).sum(dim=1)

        neg_kl_term = log_prob_hK + log_prob_pK + sum_log_sigma - log_prob_h0 - log_prob_p0

        # ELBO
        elbo = (expected_log_likelihood + neg_kl_term).mean() - kl / batch_size

        # Orthogonal regularization
        if self.orthogonal_reg:
            ortho_reg_term = torch.sum(
                (self.affine_weight.T @ self.affine_weight - torch.eye(self.h_dim, device=self.device)) ** 2
            )
            elbo -= self.orthogonal_reg * ortho_reg_term

        return elbo

    def _his(self, h_0, p_0, y):
        h = h_0
        p = p_0

        lf_eps = self.lf_eps  # Leapfrog step size
        alphas = self.alphas  # Momentum scaling factors

        for k in range(self.K):
            # Half step for momentum
            p_half = p - 0.5 * lf_eps * self._dU_dh(h, y)
            # Full step for position
            h = h + lf_eps * p_half
            # Another half step for momentum
            p_temp = p_half - 0.5 * lf_eps * self._dU_dh(h, y)
            # Momentum scaling
            p = alphas * p_temp

        return h, p

    def _dU_dh(self, h, y):
        h = h.detach().requires_grad_(True)
        # Compute potential energy U(h) = -log p(y|h) + 0.5 * ||h||^2
        log_py_h = self.gen_model.log_likelihood(h, y).sum(dim=1)  # [batch_size]
        U = -log_py_h + 0.5 * (h ** 2).sum(dim=1)  # [batch_size]
        grad_U = torch.autograd.grad(U.sum(), h)[0]  # [batch_size, h_dim]
        return grad_U

    @property
    def lf_eps(self):
        return torch.sigmoid(self.lf_reparam) * self.max_lf

    @property
    def alphas(self):
        if self.temp_method == 'free':
            return torch.sigmoid(self.alphas_reparam)
        elif self.temp_method == 'fixed':
            T_0 = 1 + torch.exp(self.T_0_reparam)
            k_vec = torch.arange(1, self.K + 1, dtype=torch.float64, device=self.device)
            k_m_1_vec = torch.arange(0, self.K, dtype=torch.float64, device=self.device)
            temp_sched = (1 - T_0) * k_vec ** 2 / self.K ** 2 + T_0
            temp_sched_m_1 = (1 - T_0) * k_m_1_vec ** 2 / self.K ** 2 + T_0
            return torch.sqrt(temp_sched / temp_sched_m_1)
        else:
            return self._alphas  # Return the buffer value

    @property
    def T_0(self):
        if self.temp_method == 'free':
            return torch.prod(self.alphas) ** (-2)
        elif self.temp_method == 'fixed':
            return 1 + torch.exp(self.T_0_reparam)
        else:
            return self._T_0  # Return the buffer value
