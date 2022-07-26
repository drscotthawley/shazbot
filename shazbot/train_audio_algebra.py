# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_train_audio_algebra.ipynb (unless otherwise specified).

__all__ = ['DiffusionDVAE', 'setup_weights', 'ad_encode_it', 'EmbedBlock', 'AudioAlgebra', 'demo', 'get_stems_faders',
           'save', 'n_params', 'freeze', 'MyPrinter', 'main']

# Cell
from prefigure.prefigure import get_all_args, push_wandb_config
from copy import deepcopy
import math
import json

import accelerate
import os, sys
import torch
import torchaudio
from torch import optim, nn, Tensor
from torch import multiprocessing as mp
from torch.nn import functional as F
from torch.utils import data as torchdata
#from torch.utils import data
from tqdm import tqdm, trange
from einops import rearrange, repeat

import wandb
from .viz import embeddings_table, pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image
import shazbot.blocks_utils as blocks_utils
from .icebox import load_audio_for_jbx, IceBoxEncoder
from .data import MultiStemDataset
import subprocess

# audio-diffusion imports
from tqdm import trange
import pytorch_lightning as pl
from diffusion.pqmf import CachedPQMF as PQMF
from diffusion.utils import PadCrop, Stereo, NormInputs
from encoders.encoders import RAVEEncoder, ResConvBlock
from nwt_pytorch import Memcodes
from dvae.residual_memcodes import ResidualMemcodes
from decoders.diffusion_decoder import DiffusionDecoder

# Cell
#audio diffusion classes
class DiffusionDVAE(nn.Module):
    def __init__(self, global_args, device):
        super().__init__()
        self.device = device

        self.pqmf_bands = global_args.pqmf_bands

        if self.pqmf_bands > 1:
            self.pqmf = PQMF(2, 70, global_args.pqmf_bands)

        self.encoder = RAVEEncoder(2 * global_args.pqmf_bands, 64, global_args.latent_dim, ratios=[2, 2, 2, 2, 4, 4])
        self.encoder_ema = deepcopy(self.encoder)

        self.diffusion = DiffusionDecoder(global_args.latent_dim, 2)
        self.diffusion_ema = deepcopy(self.diffusion)
        self.rng = torch.quasirandom.SobolEngine(1, scramble=True)
        #self.ema_decay = global_args.ema_decay

        self.num_quantizers = global_args.num_quantizers
        if self.num_quantizers > 0:
            quantizer_class = ResidualMemcodes if global_args.num_quantizers > 1 else Memcodes

            quantizer_kwargs = {}
            if global_args.num_quantizers > 1:
                quantizer_kwargs["num_quantizers"] = global_args.num_quantizers

            self.quantizer = quantizer_class(
                dim=global_args.latent_dim,
                heads=global_args.num_heads,
                num_codes=global_args.codebook_size,
                temperature=1.,
                **quantizer_kwargs
            )

            self.quantizer_ema = deepcopy(self.quantizer)



    def encode(self, *args, **kwargs):
        if self.training:
            return self.encoder(*args, **kwargs)
        return self.encoder_ema(*args, **kwargs)

    def decode(self, *args, **kwargs):
        if self.training:
            return self.diffusion(*args, **kwargs)
        return self.diffusion_ema(*args, **kwargs)

    def configure_optimizers(self):
        return optim.Adam([*self.encoder.parameters(), *self.diffusion.parameters()], lr=2e-5)


    def training_step(self, batch, batch_idx):
        reals = batch[0]

        encoder_input = reals

        if self.pqmf_bands > 1:
            encoder_input = self.pqmf(reals)

        # Draw uniformly distributed continuous timesteps
        t = self.rng.draw(reals.shape[0])[:, 0].to(self.device)

        # Calculate the noise schedule parameters for those timesteps
        alphas, sigmas = get_alphas_sigmas(get_crash_schedule(t))

        # Combine the ground truth images and the noise
        alphas = alphas[:, None, None]
        sigmas = sigmas[:, None, None]
        noise = torch.randn_like(reals)
        noised_reals = reals * alphas + noise * sigmas
        targets = noise * alphas - reals * sigmas

        # Compute the model output and the loss.
        with torch.cuda.amp.autocast():
            tokens = self.encoder(encoder_input).float()

        if self.num_quantizers > 0:
            #Rearrange for Memcodes
            tokens = rearrange(tokens, 'b d n -> b n d')

            #Quantize into memcodes
            tokens, _ = self.quantizer(tokens)

            tokens = rearrange(tokens, 'b n d -> b d n')

        with torch.cuda.amp.autocast():
            v = self.diffusion(noised_reals, t, tokens)
            mse_loss = F.mse_loss(v, targets)
            loss = mse_loss

        log_dict = {
            'train/loss': loss.detach(),
            'train/mse_loss': mse_loss.detach(),
        }

        self.log_dict(log_dict, prog_bar=True, on_step=True)
        return loss

        '''def on_before_zero_grad(self, *args, **kwargs):
        decay = 0.95 if self.current_epoch < 25 else self.ema_decay
        ema_update(self.diffusion, self.diffusion_ema, decay)
        ema_update(self.encoder, self.encoder_ema, decay)

        if self.num_quantizers > 0:
            ema_update(self.quantizer, self.quantizer_ema, decay)'''


def setup_weights(model, accelerator):
    pthfile = 'dvae-checkpoint-june9.pth'
    if not os.path.exists(pthfile):
        cmd = f'curl -C - -LO https://www.dropbox.com/s/8tcirpokhoxfo82/{pthfile}'
        process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
        output, error = process.communicate()
    #self.load_state_dict(torch.load(pthfile))
    accelerator.unwrap_model(model).load_state_dict(torch.load(pthfile))
    model = model.to(self.device)
    return model

def ad_encode_it(reals, device, dvaemodel, sample_size=32768, num_quantizers=8):
    encoder_input = reals.to(device)
    noise = torch.randn([reals.shape[0], 2, sample_size]).to(device)

    tokens = dvaemodel.encoder_ema(encoder_input)
    if num_quantizers > 0:
        #Rearrange for Memcodes
        tokens = rearrange(tokens, 'b d n -> b n d')
        tokens, _= dvaemodel.quantizer_ema(tokens)
        tokens = rearrange(tokens, 'b n d -> b d n')

    return tokens

# Cell
class EmbedBlock(nn.Module):
    def __init__(self, dims:int, **kwargs) -> None:
        super().__init__()
        self.lin = nn.Linear(dims, dims, **kwargs)
        self.act = nn.LeakyReLU()
        self.bn = nn.BatchNorm1d(dims)

    def forward(self, x: Tensor) -> Tensor:
        x = self.lin(x)
        x = rearrange(x, 'b d n -> b n d')
        x = self.bn(x)
        x = rearrange(x, 'b n d -> b d n')
        return F.leaky_relu(x, inplace=True)


class AudioAlgebra(nn.Module):
    def __init__(self, global_args, device, enc_model):
        super().__init__()
        self.device = device
        #self.encoder = encoder
        self.enc_model = enc_model
        self.dims = global_args.latent_dim
        self.sample_size = global_args.sample_size
        self.num_quantizers = global_args.num_quantizers

        self.reembedding = nn.Sequential(  # something simple at first
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            nn.Linear(self.dims,self.dims)
            )

    def forward(self,
        stems:list,   # list of torch tensors denoting (chunked) solo audio parts to be mixed together
        faders:list   # list of gain values to be applied to each stem
        ):
        """We're going to 'on the fly' mix the stems according to the fader settings and generate
        frozen-encoder embeddings for each (fader-adjusted) stem and for the total mix.
        "z0" denotes an embedding from the frozen encoder, "z" denotes re-mapped embeddings
        in (hopefully) the learned vector space"""
        with torch.cuda.amp.autocast():
            zs, zsum = [], None
            mix = torch.zeros_like(stems[0]).float()
            #print("mix.shape = ",mix.shape)
            for s, f in zip(stems, faders):
                mix_s = s * f             # audio stem adjusted by gain fader f
                with torch.no_grad():
                    #z0 = self.encoder.encode(mix_s).float()  # initial/frozen embedding/latent for that input
                    z0 = ad_encode_it(mix_s, self.device, self.enc_model, sample_size=self.sample_size, num_quantizers=self.num_quantizers)
                #print("z0.shape = ",z0.shape)  # most likely [8,32,152]
                z0 = rearrange(z0, 'b d n -> b n d')
                z = self.reembedding(z0).float()   # <-- this is the main work of the model
                zsum = z if zsum is None else zsum + z # compute the sum of all the z's. we'll end up using this in our (metric) loss as "pred"
                mix += mix_s              # save a record of full audio mix
                zs.append(z)              # save a list of individual z's

            with torch.no_grad():
                #zmix0 = self.encoder.encode(mix).float()  # compute frozen embedding / latent for the full mix
                zmix0 = ad_encode_it(mix, self.device, self.enc_model, sample_size=self.sample_size, num_quantizers=self.num_quantizers)
            zmix0 = rearrange(zmix0, 'b d n -> b n d')
            zmix = self.reembedding(zmix0).float()        # map that according to our learned re-embedding. this will be the "target" in the metric loss

        return zsum, zmix, zs, mix    # zsum = pred, zmix = target, and zs & zmix are just for extra info


    def distance(self, pred, targ):
        return torch.norm( pred - targ, dim=(1,2) ) # L2 / Frobenius / Euclidean

    def loss(self, zsum, zmix):
        with torch.cuda.amp.autocast():
            dist = self.distance(zsum, zmix)
            #print("dist = ",dist)
            #dist = rearrange(dist, 'b d n -> b (d n)') # flatten non-batch parts
            loss = dist.mean()   # mean across batch; so loss range doesn't change w/ batch_size hyperparam
        log_dict = {'loss': loss.detach()}
        return loss, log_dict

# Cell
# utils
def demo():
    return
    print("In demo placeholder")

# export
def get_stems_faders(batch, dl, maxstems=6):
    "grab some more audio stems and set faders"
    nstems = 1 + int(torch.randint(maxstems-1,(1,1))[0][0].numpy()) # an int between 1 and maxstems, PyTorch style :-/
    faders = 2*torch.rand(nstems)-1  # fader gains can be from -1 to 1
    stems = [batch]
    dl_iter = iter(dl)
    for i in range(nstems-1):
        stems.append(next(dl_iter)[0])  # [0] is because there are two items returned and audio is the first
    return stems, faders

# export
def save(accelerator, args, model, opt, epoch, step):
    "checkpointing"
    accelerator.wait_for_everyone()
    filename = f'{args.name}_{step:08}.pth'
    if accelerator.is_main_process:
        tqdm.write(f'Saving to {filename}...')
    obj = {
        'model': accelerator.unwrap_model(model).state_dict(),
        'opt': opt.state_dict(),
        'epoch': epoch,
        'step': step
    }
    accelerator.save(obj, filename)

def n_params(module):
    """Returns the number of trainable parameters in a module."""
    return sum(p.numel() for p in module.parameters())


def freeze(model):
    for param in model.parameters():
        param.requires_grad = False


class MyPrinter():
    def __init__(self, accelerator):
        self.accelerator = accelerator
    def __call__(self,s):
        if self.accelerator.is_main_process:
            print(s, flush=True)

# Cell
def main():

    args = get_all_args()
    torch.manual_seed(args.seed)

    try:
        mp.set_start_method(args.start_method)
    except RuntimeError:
        pass

    accelerator = accelerate.Accelerator()
    device = accelerator.device
    myprint = MyPrinter(accelerator)
    myprint(f'Using device: {device}')

    encoder_choices = ['ad','icebox']
    encoder_choice = encoder_choices[0]
    myprint(f"Using {encoder_choice} as encoder")
    if 'icebox' == encoder_choice:
        args.latent_dim = 64  # overwrite latent_dim with what Jukebox requires
        encoder = IceBoxEncoder(args, device)
    elif 'ad' == encoder_choice:
        dvae = DiffusionDVAE(args, device)
        #dvae = setup_weights(dvae, accelerator, device)
        #encoder = dvae.encoder
        #freeze(dvae)

    myprint("Setting up AA model")
    aa_model = AudioAlgebra(args, device, dvae)

    myprint(f'  AA Model Parameters: {blocks_utils.n_params(aa_model)}')

    # If logging to wandb, initialize the run
    use_wandb = accelerator.is_main_process and args.name
    if use_wandb:
        import wandb
        config = vars(args)
        config['params'] = blocks_utils.n_params(aa_model)
        wandb.init(project=args.name, config=config, save_code=True)

    opt = optim.Adam([*aa_model.reembedding.parameters()], lr=4e-5)

    myprint("Setting up dataset")
    train_set = MultiStemDataset([args.training_dir], args)
    train_dl = torchdata.DataLoader(train_set, args.batch_size, shuffle=True,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True)

    myprint("Calling accelerator.prepare")
    aa_model, opt, train_dl, dvae = accelerator.prepare(aa_model, opt, train_dl, dvae)

    myprint("Setting up frozen encoder model weights")
    dvae = setup_weights(dvae, accelerator, device)
    freeze(accelerator.unwrap_model(dvae))
    #encoder = dvae.encoder

    myprint("Setting up wandb")
    if use_wandb:
        wandb.watch(aa_model)

    myprint("Checking for checkpoint")
    if args.ckpt_path:
        ckpt = torch.load(args.ckpt_path, map_location='cpu')
        accelerator.unwrap_model(aa_model).load_state_dict(ckpt['model'])
        opt.load_state_dict(ckpt['opt'])
        epoch = ckpt['epoch'] + 1
        step = ckpt['step'] + 1
        del ckpt
    else:
        epoch = 0
        step = 0

    # all set up, let's go
    myprint("Let's go...")
    try:
        while True:  # training loop
            #print(f"Starting epoch {epoch}")
            for batch in tqdm(train_dl, disable=not accelerator.is_main_process):
                batch = batch[0]  # first elem is the audio, 2nd is the filename which we don't need
                #if accelerator.is_main_process: print(f"e{epoch} s{step}: got batch. batch.shape = {batch.shape}")
                opt.zero_grad()

                # "batch" is actually not going to have all the data we want. We could rewrite the dataloader to fix this,
                # but instead I just added get_stems_faders() which grabs "even more" audio to go with "batch"
                stems, faders = get_stems_faders(batch, train_dl)

                zsum, zmix, zs, mix = accelerator.unwrap_model(aa_model).forward(stems,faders)
                loss, log_dict = accelerator.unwrap_model(aa_model).loss(zsum, zmix)
                accelerator.backward(loss)
                opt.step()

                if accelerator.is_main_process:
                    if step % 25 == 0:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}')

                    if use_wandb:
                        log_dict = {
                            **log_dict,
                            'epoch': epoch,
                            'loss': loss.item(),
                            #'lr': sched.get_last_lr()[0],
                        }
                        wandb.log(log_dict, step=step)

                    if step % args.demo_every == 0:
                        demo()

                if step > 0 and step % args.checkpoint_every == 0:
                    save(accelerator, args, aa_model, opt, epoch, step)

                step += 1
            epoch += 1
    except RuntimeError as err:  # ??
        import requests
        import datetime
        ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        resp = requests.get('http://169.254.169.254/latest/meta-data/instance-id')
        myprint(f'ERROR at {ts} on {resp.text} {device}: {type(err).__name__}: {err}', flush=True)
        raise err
    except KeyboardInterrupt:
        pass

# Cell
# Not needed if listed in console_scripts in settings.ini
if __name__ == '__main__' and "get_ipython" not in dir():  # don't execute in notebook
    main()