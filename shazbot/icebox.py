# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/icebox.ipynb.

# %% auto 0
__all__ = ['JUKEBOX_SAMPLE_RATE', 'init_jukebox_sample_rate', 'stereo', 'audio_for_jbx', 'load_audio_for_jbx', 'IceBoxModel',
           'batch_it_crazy', 'main']

# %% ../nbs/icebox.ipynb 2
import torch 
from torch import nn 
from torch import multiprocessing as mp
import torch.distributed as dist
from torch.nn import functional as F
import torchaudio
from jukebox.make_models import make_vqvae, make_prior, MODELS, make_model
from jukebox.hparams import Hyperparams, setup_hparams
import os
import accelerate
from aeiou.hpc import get_accel_config, HostPrinter
from aeiou.viz import embeddings_table, pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image, plot_jukebox_embeddings
import librosa

# %% ../nbs/icebox.ipynb 4
#JUKEBOX_SAMPLE_RATE = 44100  # ethan's original
JUKEBOX_SAMPLE_RATE = None

def init_jukebox_sample_rate(
    sr=44100  # sample rate in Hz. OpenAI's pretrained Jukebox weights are for 44100
    ): 
    "SHH added this util to preserve rest of code minimall-modified"
    global JUKEBOX_SAMPLE_RATE
    JUKEBOX_SAMPLE_RATE = sr
    return

def stereo(signal):
    signal_shape = signal.shape
    if len(signal_shape) == 1: # s -> 2, s
        signal = signal.unsqueeze(0).repeat(2, 1)
    elif len(signal_shape) == 2:
        if signal_shape[0] == 1: #1, s -> 2, s
            signal = signal.repeat(2, 1)
        elif signal_shape[0] > 2: #?, s -> 2,s
            signal = signal[:2, :]  
    return signal 

def audio_for_jbx(audio, trunc_sec=None, device=None):
    """Readies an audio TENSOR for Jukebox."""
    if audio.ndim == 1:
        audio = audio[None]
        audio = audio.mean(axis=0)
    #print("1 audio.shape = ",audio.shape)

    # normalize audio
    norm_factor = torch.abs(audio).max()
    if norm_factor > 0:
        audio /= norm_factor

    if trunc_sec is not None:  # truncate sequence
        audio = audio[: int(JUKEBOX_SAMPLE_RATE * trunc_sec)]

    audio = torch.unsqueeze(audio, 0)  # batch dimension
    audio = torch.unsqueeze(audio, dim=-1)  # another dimension ?
    return audio


def load_audio_for_jbx(path, offset=0.0, dur=None, trunc_sec=None, device=None):
    """Loads a path for use with Jukebox."""
    audio, sr = librosa.load(path, sr=None, offset=offset, duration=dur)

    if JUKEBOX_SAMPLE_RATE is None: init_jukebox_sample_rate()

    if sr != JUKEBOX_SAMPLE_RATE:
        audio = librosa.resample(audio, sr, JUKEBOX_SAMPLE_RATE)
    audio = torch.from_numpy(audio)
    #print("0, audio.shape = ",audio.shape)
    return audio_for_jbx(audio, trunc_sec, device=device)

# %% ../nbs/icebox.ipynb 6
class IceBoxModel(nn.Module):
    def __init__(self, global_args, device, port=9500):
        super().__init__()

        n_io_channels = 2
        n_feature_channels = 8

        # for making Jukebox work with multi-GPU runs
        rank = global_args.rank
        #local_rank, device = int(os.getenv('RANK')), int(os.getenv('LOCAL_RANK')), device

        # torch.distributed info set at top-level training script
        #dist_url = f"tcp://127.0.0.1:{port}"  # Note port may differ on different machines
        #dist.init_process_group(backend="nccl")

        self.hps = Hyperparams()
        assert global_args.sample_rate == 44100, "Jukebox was pretrained at 44100 Hz."
        self.hps.sr = global_args.sample_rate #44100
        self.hps.levels = 3
        self.hps.hop_fraction = [.5,.5,.125]

        vqvae = "vqvae"
        self.vqvae = make_vqvae(setup_hparams(vqvae, dict(sample_length = 1048576)), device)
        for param in self.vqvae.parameters():  # FREEZE IT.  "IceBox"
            param.requires_grad = False
            
        self.dummy = nn.Linear(1,1) # just to allow DistributedDataParallel

        self.encoder = self.vqvae.encode
        self.decoder = self.vqvae.decode

        latent_dim = 64 # global_args.latent_dim. Jukebox is 64
        io_channels = 2#1 # 2.  Jukebox is mono but we decode in stereo
 
    def encode(self, *args, **kwargs):
        return self.encoder(*args, **kwargs)

    def decode(self, *args, **kwargs):
        return self.decoder(*args, **kwargs)   

# %% ../nbs/icebox.ipynb 7
def batch_it_crazy(x, win_len):
    "(pun intended) Chop up long sequence into a batch of win_len windows"
    x_len = x.size()[-1]
    n_windows = (x_len // win_len) + 1
    pad_amt = win_len * n_windows - x_len  # pad end w. zeros to make lengths even when split
    xpad = F.pad(x, (0, pad_amt))
    return rearrange(xpad, 'd (b n) -> b d n', n=win_len)

# %% ../nbs/icebox.ipynb 9
def main():
    #from dotmap import DotMap  # only used for setting some args
    from prefigure.prefigure import get_all_args, push_wandb_config

    try:
        args = get_all_args()
    except:
        print("Can't read config file. Exiting")
        return 
        
    torch.manual_seed(args.seed)

    try:
        mp.set_start_method(args.start_method)
    except RuntimeError:
        pass

    accelerator = accelerate.Accelerator()
    device = accelerator.device
    hprint = HostPrinter(accelerator)
    hprint(f"device = {device}")
    hprint(f"accelerator = {accelerator}")
    ac = get_accel_config()
    hprint(f"ac = {ac}")
    port = ac['main_process_port']
    #args = DotMap()
    args.name = 'icebox-test'
    args.sample_rate = 44100
    args.rank = ac['machine_rank']
    os.environ["RANK"] = str(args.rank)
    if device != 'cpu':
        icebox = IceBoxModel(args, device, port=port)
        hprint("IceBoxModel config finished!")
    else:
        print("can't start up icebox because no GPUs are available.")

    icebox = accelerator.prepare(icebox)

    hprint(f"Loading audio")
    input_filename = 'test_audio.wav'
    input_audio = load_audio_for_jbx(input_filename).to(device)
    #demo_reals = batch_it_crazy(input_audio, args.sample_size)


    hprint(f"Encoding audio")
    with torch.cuda.amp.autocast():
        zs = accelerator.unwrap_model(icebox).encode(input_audio)
        hprint(f"  len(zs) = {len(zs)}")
        for i, z in enumerate(zs):
            hprint(f"  zs[{i}].shape = {z.shape}")

    hprint(f"Decoding audio")
    decoded = accelerator.unwrap_model(icebox).decode(zs).transpose(-2, -1)
    hprint(f"  decoded.shape = {decoded.shape}")
    output_audio = torch.squeeze(decoded, dim=0).cpu() 
    #output_audio *=  0.2 # it's too loud for some reason
    hprint(f"  output_audio.shape = {output_audio.shape}")
    output_filename = 'test_audio_out.wav'

    hprint(f"Saving output audio {output_filename}...")
    torchaudio.save(output_filename, output_audio, args.sample_rate)

    input_audio = torch.squeeze(input_audio, dim=-1).cpu()
    hprint(f"input_audio.shape = {input_audio.shape}")

    use_wandb = accelerator.is_main_process and args.name
    if use_wandb:
        import wandb
        hprint("Now sending to WandB")
        config = vars(args)
        wandb.init(project=args.name, config=config, save_code=True)
        log_dict = {}

        log_dict['input_audio']  = wandb.Audio(input_filename, sample_rate=args.sample_rate, caption='input_audio')
        log_dict['output_audio'] = wandb.Audio(output_filename, sample_rate=args.sample_rate, caption='output_audio')
        log_dict[f'input_melspec_left'] = wandb.Image(audio_spectrogram_image(input_audio, print=hprint))
        log_dict[f'output_melspec_left'] = wandb.Image(audio_spectrogram_image(output_audio, print=hprint))

        if False:
            hprint("Logging embeddings...")
            log_dict[f'zplots'] = wandb.log(plot_jukebox_embeddings(zs))
            #log_dict[f'zplots2'] = wandb.Image(plot_jukebox_embeddings(zs))

        # make a 3d cube of jukebox embeddings
        em_plot_arr =torch.zeros((3,zs[0].shape[-1]))
        em_plot_arr[0,:] = zs[0][0]
        em_plot_arr[1,:] = zs[1][0].repeat(4)
        em_plot_arr[2,:-4] = zs[2][0].repeat(4*4)
        absmax = torch.max(torch.abs(em_plot_arr))
        em_plot_arr = (em_plot_arr - em_plot_arr.mean()) / absmax # normalize a bit
        log_dict[f'emb_3d_color=time'] = pca_point_cloud(em_plot_arr.unsqueeze(0), color_scheme='n')

        hprint(f"log_dict = {log_dict}")
        wandb.log(log_dict, step=0)

    hprint("\n--------------------\nFinished!\n-------------")
    
if __name__ == '__main__':  # often this will only be called for testing
    main() 
