import torch
import torch.distributed as dist
from models import DiT_models
from download import find_model
from diffusion import create_diffusion
from tqdm import tqdm
import argparse
from einops import repeat
from transformers import T5ForConditionalGeneration, T5Tokenizer
from train_autoencoder import ldmol_autoencoder
from utils import AE_SMILES_decoder, molT5_encoder, regexTokenizer
import time
from dataset import smi_txt_dataset
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from metrics import molfinger_evaluate, mol_evaluate
from rdkit import Chem


@torch.no_grad()
def main(args):
    """
    Run sampling.
    """
    torch.backends.cuda.matmul.allow_tf32 = args.tf32  # True: fast but may lead to some small numerical differences
    assert torch.cuda.is_available(), "Sampling with DDP requires at least one GPU. sample.py supports CPU-only usage"
    torch.set_grad_enabled(False)

    # Setup DDP:
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()
    seed = args.global_seed * dist.get_world_size() + rank
    torch.manual_seed(seed)
    torch.cuda.set_device(device)
    print(f"Starting rank={rank}, seed={seed}, world_size={dist.get_world_size()}.")

    if args.ckpt is None:
        raise ValueError("Please specify a checkpoint path with --ckpt.")

    # Load model:
    latent_size = 127
    in_channels = 64  # 64
    cross_attn = 768
    if args.text_encoder_name == 'llama2':
        condition_dim = 4096
    elif args.text_encoder_name == 'molt5':
        condition_dim = 1024
    model = DiT_models[args.model](
        input_size=latent_size,
        in_channels=in_channels,
        cross_attn=cross_attn,
        condition_dim=condition_dim,
    ).to(device)
    # Auto-download a pre-trained model or load a custom DiT checkpoint from train.py:
    ckpt_path = args.ckpt
    state_dict = find_model(ckpt_path)
    msg = model.load_state_dict(state_dict, strict=False)
    if rank == 0:   print('DiT from ', ckpt_path, msg)
    model.eval()  # important!
    diffusion = create_diffusion(str(args.num_sampling_steps))

    ae_config = {
        'bert_config_decoder': './config_decoder.json',
        'bert_config_encoder': './config_encoder.json',
        'embed_dim': 256,
    }
    tokenizer = regexTokenizer(vocab_path='./vocab_bpe_300_sc.txt', max_len=127)#newtkn
    ae_model = ldmol_autoencoder(config=ae_config, no_train=True, tokenizer=tokenizer, use_linear=True)
    if args.vae:
        checkpoint = torch.load(args.vae, map_location='cpu')
        try:
            state_dict = checkpoint['model']
        except:
            state_dict = checkpoint['state_dict']
        msg = ae_model.load_state_dict(state_dict, strict=False)
        if rank == 0:   print('autoencoder', args.vae, msg)
    for param in ae_model.parameters():
        param.requires_grad = False
    del ae_model.text_encoder2
    ae_model = ae_model.to(device)
    ae_model.eval()
    if rank == 0:   print(f'AE #parameters: {sum(p.numel() for p in ae_model.parameters())}, #trainable: {sum(p.numel() for p in ae_model.parameters() if p.requires_grad)}')
    # vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{args.vae}").to(device)

    assert args.cfg_scale >= 1.0, "In almost all cases, cfg_scale be >= 1.0"
    using_cfg = args.cfg_scale > 1.0

    text_encoder = T5ForConditionalGeneration.from_pretrained('laituan245/molt5-large-caption2smiles').to(device)
    text_tokenizer = T5Tokenizer.from_pretrained("laituan245/molt5-large-caption2smiles", model_max_length=512)
    del text_encoder.decoder

    for param in text_encoder.parameters():
        param.requires_grad = False
    text_encoder.eval()
    if rank == 0:
        print(f'text encoder #parameters: {sum(p.numel() for p in text_encoder.parameters())}, #trainable: {sum(p.numel() for p in text_encoder.parameters() if p.requires_grad)}')

    dist.barrier()

    prompt_null = "no dsecription."
    biot5_embed_null, mask_null = molT5_encoder([prompt_null], text_encoder, text_tokenizer, args.description_length, device)

    biot5_embed_null = biot5_embed_null.to(device).to(torch.float32)
    mask_null = mask_null.to(device).bool()

    test_dataset = smi_txt_dataset(['./data/chebi_20/test_parsed.txt'], data_length=None, shuffle=False, unconditional=False, raw_description=True)
    if rank == 0:   print('#data:', len(test_dataset))

    sampler = DistributedSampler(
        test_dataset,
        num_replicas=dist.get_world_size(),
        rank=rank,
        shuffle=True,
        seed=args.global_seed
    )
    loader = DataLoader(
        test_dataset,
        batch_size=int(args.per_proc_batch_size),
        shuffle=False,
        sampler=sampler,
        num_workers=8,
        pin_memory=True,
        drop_last=False
    )

    st = time.time()
    sampler.set_epoch(0)
    loader = tqdm(loader, miniters=1) if rank == 0 else loader

    if rank == 0:
        with open('./generated_molecules_t2m.txt', 'w') as f:
            pass
    for x, y in loader:
        # Sample inputs:
        z = torch.randn(len(x), model.in_channels, latent_size, 1, device=device)

        biot5_embed, pad_mask = molT5_encoder(y, text_encoder, text_tokenizer, args.description_length, device)

        y_cond = biot5_embed.to(device).type(torch.float32)
        pad_mask_cond = pad_mask.to(device).bool()
    
        y_null = repeat(biot5_embed_null, '1 L D -> B L D', B=len(x))
        pad_mask_null = repeat(mask_null, '1 L -> B L', B=len(x))

        # Setup classifier-free guidance:
        if using_cfg:
            z = torch.cat([z, z], 0)
            y = torch.cat([y_cond, y_null], 0)
            pad_mask = torch.cat([pad_mask_cond, pad_mask_null], 0)
            model_kwargs = dict(y=y, pad_mask=pad_mask, cfg_scale=args.cfg_scale)
            sample_fn = model.forward_with_cfg
        else:
            model_kwargs = dict(y=y_cond, pad_mask=pad_mask)
            sample_fn = model.forward

        # Sample images:
        samples = diffusion.p_sample_loop(
            sample_fn, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=False, device=device
        )
        if using_cfg:
            samples, _ = samples.chunk(2, dim=0)  # Remove null class samples

        samples = samples.squeeze(-1).permute((0, 2, 1))
        samples = AE_SMILES_decoder(samples, ae_model, stochastic=False, k=1)

        # Save samples to disk as individual .png files
        assert len(samples) == len(x)
        with open('./generated_molecules_t2m.txt', 'a') as f:
            for i, s in enumerate(samples):
                f.write(x[i].replace('[CLS]', '')+'\t'+s+'\n')

    # Make sure all processes have finished saving their samples before attempting to convert to .npz
    dist.barrier()
    if rank == 0:
        print('time:', time.time()-st)
        print('done')
        with open('./generated_molecules_t2m.txt', 'r') as f:
            lines = f.readlines()

        appeared = []
        line = []
        for l in lines:
            if l.split('\t')[0] not in appeared:
                appeared.append(l.split('\t')[0])
                line.append(l)
        lines = line

        print(len(lines))
        lines = [l.strip() for l in lines]
        target, pred = [], []
        for l in lines:
            try:
                l = l.split('\t')
                target.append(l[0])
                if len(l)!=2:
                    pred.append('Q')
                else:
                    pred.append(l[1])
            except:
                print(l)

        pred = [Chem.MolToSmiles(Chem.MolFromSmiles(l), isomericSmiles=True, canonical=True) if Chem.MolFromSmiles(l) else l for l in pred]
        target = [Chem.MolToSmiles(Chem.MolFromSmiles(l), isomericSmiles=True, canonical=True) for l in target]

        _ = mol_evaluate(target, pred, verbose=True)[-1]
        molfinger_evaluate(target, pred, verbose=True)
    dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, choices=list(DiT_models.keys()), default="LDMol")
    parser.add_argument("--vae", type=str, default="./Pretrain/checkpoint_autoencoder.ckpt")  # Choice doesn't affect training
    parser.add_argument("--text-encoder-name", type=str, default="molt5")
    parser.add_argument("--description-length", type=int, default=256)
    parser.add_argument("--per-proc-batch-size", type=int, default=64)
    parser.add_argument("--cfg-scale",  type=float, default=7.5)
    parser.add_argument("--num-sampling-steps", type=int, default=100)
    parser.add_argument("--global-seed", type=int, default=0)
    parser.add_argument("--tf32", action=argparse.BooleanOptionalAction, default=True,
                        help="By default, use TF32 matmuls. This massively accelerates sampling on Ampere GPUs.")
    parser.add_argument("--ckpt", type=str, default=None,
                        help="Optional path to a DiT checkpoint (default: auto-download a pre-trained DiT-XL/2 model).")
    args = parser.parse_args()
    main(args)
