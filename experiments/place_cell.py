import argparse
import numpy as np
import tqdm
import torch

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '../'))

import gpvae
from data.place_cell import load
from gpvae.utils.misc import save
from gpvae.utils.dataset import TupleDataset
from gpvae.kernels.kernels import SEKernel
from gpvae.likelihoods.gaussian import NNHomoGaussian, NNHeteroGaussian
from gpvae.likelihoods.poisson import NNPoissonCount
from gpvae.models.models import VAE, GPVAE, SGPVAE, SR_nlGPFA, SR_nlHGPFA
import gpvae.utils.evaluation as evaluation

torch.set_default_dtype(torch.float64)
DEVICE = (
    torch.device('cuda')
    if torch.cuda.is_available()
    #else torch.device('mps')
    #if torch.backends.mps.is_available()
    else torch.device('cpu')
)

def main(args):
    _, train, test, _ = load(args.session_id, num_points=args.num_points, binsize=args.data_binsize)
    x = np.array(train.index)
    y = np.array(train, dtype='float64')

    print(f'{args.session_id} | num cells: {y.shape[1]} | num obs: {y.shape[0]}')

    x_test, y_test = np.array(test.index), np.array(test, dtype='float64')

    x = torch.tensor(x)
    y = torch.tensor(y)

    dataset = TupleDataset(x, y, missing=False)
    loader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size, shuffle=(not args.contiguous))

    x_test = torch.tensor(x_test)
    y_test = torch.tensor(y_test)
    test_dataset = TupleDataset(x_test, y_test, missing=False)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=True)

    # kernel function
    kernel = SEKernel(lengthscale=args.init_lengthscale, scale=args.init_scale).to(DEVICE)

    if args.h_dim == 0 and args.model == 'sr-nlgpfa':
        args.h_dim = y.shape[-1]
    if args.h_dim == 0 and args.model == 'sr-nlhgpfa':
        args.h_dim = y.shape[-1]
    # generative model
    if args.likelihood == 'nn-poisson':
        print('Using NN Poisson-count generative model')
        gen_model = NNPoissonCount(in_dim=args.latent_dim if not args.h_dim else args.h_dim, out_dim=y.shape[1],
                                   hidden_dims=args.dec_dims).to(DEVICE)
    elif args.likelihood == 'nn-heter-gaussian':
        print('Using NN Gaussian generative model with Heteroscedastic noise')
        gen_model = NNHeteroGaussian(in_dim=args.latent_dim if not args.h_dim else args.h_dim, out_dim=y.shape[1],
                                     hidden_dims=args.dec_dims).to(DEVICE)
    else:
        print('Using NN Gaussian generative model with Homoscedastic noise')
        gen_model = NNHomoGaussian(in_dim=args.latent_dim if not args.h_dim else args.h_dim, out_dim=y.shape[1],
                                    hidden_dims=args.dec_dims, sigma=args.sigma).to(DEVICE)

    latent_dim = args.latent_dim if not args.h_dim else args.h_dim

    # recognition model
    recog_model = NNHeteroGaussian(in_dim=y.shape[1], out_dim=latent_dim if not args.h_dim else args.h_dim,
            hidden_dims=args.enc_dims, min_sigma=args.min_sigma, init_sigma=args.initial_sigma).to(DEVICE)

    # main model
    if args.model == 'gpvae':
        model = GPVAE(recog_model=recog_model, gen_model=gen_model, latent_dim=latent_dim, kernel=kernel,
                      add_jitter=args.add_jitter, device=DEVICE)
    elif args.model == 'sgpvae':
        z_init = torch.linspace(0, x[-1].item(), steps=args.num_inducing).unsqueeze(1)
        model = SGPVAE(recog_model=recog_model, gen_model=gen_model, latent_dim=latent_dim, kernel=kernel,
                      z=z_init, add_jitter=args.add_jitter, fixed_inducing=args.fixed_inducing, device=DEVICE)
    elif args.model == 'sr-nlgpfa':
        z_init = torch.linspace(0, x[-1].item(), steps=args.num_inducing).unsqueeze(1)
        init_affine_weight = torch.randn((args.h_dim, args.latent_dim))
        init_affine_bias = torch.zeros((args.h_dim, ))
        model = SR_nlGPFA(recog_model=recog_model, gen_model=gen_model, latent_dim=args.latent_dim, kernel=kernel,
                      z=z_init, add_jitter=args.add_jitter, fixed_inducing=args.fixed_inducing, h_dim=args.h_dim,
                      affine_weight=init_affine_weight, affine_bias=init_affine_bias, device=DEVICE,
                      orthogonal_reg=args.orthogonal_reg)
    elif args.model == 'vae':
        model = VAE(recog_model, gen_model, args.latent_dim, device=DEVICE)



    elif args.model == 'sr-nlhgpfa':

        z_init = torch.linspace(0, x[-1].item(), steps=args.num_inducing).unsqueeze(1)

        init_affine_weight = torch.randn((args.h_dim, args.latent_dim))

        init_affine_bias = torch.zeros((args.h_dim,))

        model = SR_nlHGPFA(

            recog_model=recog_model,

            gen_model=gen_model,

            latent_dim=args.latent_dim,

            kernel=kernel,

            z=z_init,

            add_jitter=args.add_jitter,

            fixed_inducing=args.fixed_inducing,

            h_dim=args.h_dim,

            affine_weight=init_affine_weight,

            affine_bias=init_affine_bias,

            device=DEVICE,

            orthogonal_reg=args.orthogonal_reg,

            K=args.K,  # Number of leapfrog steps

            init_lf=args.init_lf,

            max_lf=args.max_lf,

            temp_method=args.temp_method

        )
    else:
        raise NotImplementedError

    optimiser = torch.optim.Adam(model.parameters(), lr=args.lr)
    epoch_iter = tqdm.tqdm(range(args.epochs), desc='Epoch')
    num_batches = y.shape[0] // args.batch_size + 1

    for epoch in epoch_iter:
        losses = []
        counter = 0
        model.train()
        for batch in loader:
            x_b, y_b, _ = batch
            x_b = x_b.to(DEVICE)
            y_b = y_b.to(DEVICE)

            optimiser.zero_grad()

            # Adjusted the free_energy call
            loss = -model.free_energy(x_b, y_b, num_samples=1)

            loss.backward()
            optimiser.step()

            losses.append(loss.item())

            counter += 1

        epoch_iter.set_postfix(loss=np.mean(losses))

        if epoch % args.cache_freq == 0:
            model.eval()
            fe_test = 0.0
            smse_test = 0.0
            for test_batch in test_loader:
                x_test_b, y_test_b, _ = test_batch
                x_test_b = x_test_b.to(DEVICE)
                y_test_b = y_test_b.to(DEVICE)

                # Adjusted the free_energy call
                fe_b = model.free_energy(x_test_b, y_test_b, num_samples=1)
                fe_test = fe_test + fe_b * x_test_b.shape[0]

                mean, sigma = model.generation(x=x_test_b, y=y_test_b, num_samples=100)[:2]

                mean, sigma = mean.cpu().detach().numpy(), sigma.cpu().detach().numpy()

                smse_b = evaluation.smse(mean, y_test_b.detach().cpu().numpy()).mean()

                smse_test = smse_test + smse_b * x_test_b.shape[0]
            smse_test = smse_test / test_dataset.x.shape[0]

            tqdm.tqdm.write('TEST-Free-Energy: {:.3f} | TRAIN-Free-Energy: {:.3f} | TEST-SMSE: {:.3f}'.format(fe_test.item(), \
                -np.mean(losses) * dataset.y.shape[0], smse_test))

    model.eval()
    fe_test = 0.0
    smse_test = 0.0
    for test_batch in test_loader:
        x_test_b, y_test_b, _ = test_batch
        x_test_b = x_test_b.to(DEVICE)
        y_test_b = y_test_b.to(DEVICE)
        fe_b = model.free_energy(x_test_b, y_test_b, num_samples=1)
        fe_test = fe_test + fe_b * x_test_b.shape[0]

        mean, sigma = model.generation(x=x_test_b, y=y_test_b, num_samples=100)[:2]

        mean, sigma = mean.cpu().detach().numpy(), sigma.cpu().detach().numpy()

        smse_b = evaluation.smse(mean, y_test_b.detach().cpu().numpy()).mean()

        smse_test = smse_test + smse_b * x_test_b.shape[0]
    smse_test = smse_test / test_dataset.x.shape[0]

    print('\nSMSE: {:.3f}'.format(smse_test))
    print('Free-Energy: {:.3f}'.format(fe_test))

    if args.save:
        metrics = {'TEST-Free-Energy': fe_test, 'SMSE': smse_test}
        save(vars(args), metrics, model, seed=args.seed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Kernel.
    parser.add_argument('--init_lengthscale', default=0.05, type=float)
    parser.add_argument('--init_scale', default=1., type=float)

    # GPVAE.
    parser.add_argument('--model', default='sr-nlgpfa')
    parser.add_argument('--likelihood', default='nn-poisson', type=str)
    parser.add_argument('--latent_dim', default=3, type=int)
    parser.add_argument('--dec_dims', default=[256, 256], nargs='+',
                        type=int)
    parser.add_argument('--sigma', default=0.1, type=float)
    parser.add_argument('--enc_dims', default=[256, 256], nargs='+', type=int)
    parser.add_argument('--rho_dims', default=[20], nargs='+', type=int)
    parser.add_argument('--inter_dim', default=20, type=int)
    parser.add_argument('--num_inducing', default=64, type=int)
    parser.add_argument('--fixed_inducing', action='store_true')
    parser.add_argument('--add_jitter', action='store_true')
    parser.add_argument('--min_sigma', default=1e-3, type=float)
    parser.add_argument('--initial_sigma', default=.1, type=float)

    # Training.
    parser.add_argument('--epochs', default=400, type=int)
    parser.add_argument('--cache_freq', default=100, type=int)
    parser.add_argument('--batch_size', default=128, type=int)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--orthogonal_reg', default=0.0, type=float)

    # General.
    parser.add_argument('--save', action='store_true')
    parser.add_argument('--results_dir', default='./_results/place_cell/', type=str)
    parser.add_argument('--seed', default=0, type=int)

    # AEA-GPFA
    parser.add_argument('--h_dim', default=20, type=int)

    # Spiking data parsing
    parser.add_argument('--session_id', default='20151031_R2336_track1', type=str)
    parser.add_argument('--binsize', default=0.1, type=float)
    parser.add_argument('--num_points', default=6000, type=int)
    parser.add_argument('--data_binsize', default=0.1, type=float)

    # Contiguous subsequences for training
    parser.add_argument('--contiguous', action='store_true')

    # Hamiltonian Importance Sampling parameters
    parser.add_argument('--K', default=10, type=int, help='Number of leapfrog steps in HMC')
    parser.add_argument('--init_lf', default=0.1, type=float, help='Initial leapfrog step size')
    parser.add_argument('--max_lf', default=0.5, type=float, help='Maximum leapfrog step size')
    parser.add_argument('--temp_method', default='none', type=str, help='Temperature scaling method: none, free, fixed')

    # Add new arguments to the parser
    parser.add_argument('--init_alpha', default=0.5, type=float, help='Initial alpha value for temp_method "free"')
    parser.add_argument('--init_T_0', default=2.0, type=float, help='Initial T_0 value for temp_method "fixed"')

    args = parser.parse_args()

    main(args)