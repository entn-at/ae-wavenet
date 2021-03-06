# Full Autoencoder model
from sys import stderr
from hashlib import md5
import numpy as np
from pickle import dumps
import torch
from torch import nn
from torch.nn.modules import loss
from scipy.cluster.vq import kmeans

import ae_bn
import mfcc
import parse_tools  
import vconv
import util
import vq_bn
import vqema_bn
import vae_bn
import wave_encoder as enc
import wavenet as dec 


class AutoEncoder(nn.Module):
    """
    Full Autoencoder model.  The _initialize method allows us to seamlessly initialize
    from __init__ or __setstate__ 
    """
    def __init__(self, opts, dataset):
        opts_dict = vars(opts)
        enc_params = parse_tools.get_prefixed_items(opts_dict, 'enc_')
        bn_params = parse_tools.get_prefixed_items(opts_dict, 'bn_')
        dec_params = parse_tools.get_prefixed_items(opts_dict, 'dec_')
        dec_params['n_speakers'] = dataset.num_speakers()

        self.init_args = {
                'enc_params': enc_params,
                'bn_params': bn_params,
                'dec_params': dec_params,
                'n_mel_chan': dataset.num_mel_chan(),
                'training': opts.training
                }
        self._initialize()

    def _initialize(self):
        super(AutoEncoder, self).__init__() 
        enc_params = self.init_args['enc_params']
        bn_params = self.init_args['bn_params']
        dec_params = self.init_args['dec_params']
        n_mel_chan = self.init_args['n_mel_chan']
        training = self.init_args['training']

        # the "preprocessing"
        self.preprocess = dec.PreProcess(n_quant=dec_params['n_quant'])

        self.encoder = enc.Encoder(n_in=n_mel_chan, parent_vc=None, **enc_params)

        bn_type = bn_params['type']
        bn_extra = dict((k, v) for k, v in bn_params.items() if k != 'type')
    
        # In each case, the objective function's 'forward' method takes the
        # same arguments.
        if bn_type == 'vqvae':
            self.bottleneck = vq_bn.VQ(**bn_extra, n_in=enc_params['n_out'])
            self.objective = vq_bn.VQLoss(self.bottleneck)

        elif bn_type == 'vqvae-ema':
            self.bottleneck = vqema_bn.VQEMA(**bn_extra, n_in=enc_params['n_out'],
                    training=training)
            self.objective = vqema_bn.VQEMALoss(self.bottleneck)

        elif bn_type == 'vae':
            # mu and sigma members  
            self.bottleneck = vae_bn.VAE(n_in=enc_params['n_out'],
                    n_out=bn_params['n_out'])
            self.objective = vae_bn.SGVBLoss(self.bottleneck,
                    free_nats=bn_params['free_nats']) 

        elif bn_type == 'ae':
            self.bottleneck = ae_bn.AE(n_out=bn_extra['n_out'], n_in=enc_params['n_out'])
            self.objective = ae_bn.AELoss(self.bottleneck, 0.001) 

        else:
            raise InvalidArgument('bn_type must be one of "ae", "vae", or "vqvae"')

        self.bn_type = bn_type
        self.decoder = dec.WaveNet(
                **dec_params,
                parent_vc=self.encoder.vc['end'],
                n_lc_in=bn_params['n_out']
                )
        self.vc = self.decoder.vc
        self.decoder.post_init()

    def post_init(self, dataset):
        self.encoder.set_parent_vc(dataset.mfcc_vc)
        self._init_geometry(dataset.window_batch_size)

    def _init_geometry(self, batch_win_size):
        """
        Initializes lengths and trimming needed to produce batch_win_size
        output
        
        self.enc_in_len - encoder input length (timesteps)
        self.dec_in_len - decoder input length (timesteps)
        self.trim_ups_out - trims decoder lc_dense before use  
        self.trim_dec_out - trims wav_dec_input to wav_dec_output
        self.trim_dec_in  - trims wav_enc_input to wav_dec_input

        The trimming vectors are needed because, due to striding geometry,
        output tensors cannot be produced in single-increment sizes, therefore
        must be over-produced in some cases.
        """
        # Calculate max length of mfcc encoder input and wav decoder input
        w = batch_win_size
        mfcc_vc = self.encoder.vc['beg'].parent
        end_enc_vc = self.encoder.vc['end']
        end_ups_vc = self.decoder.vc['last_upsample']
        beg_grcc_vc = self.decoder.vc['beg_grcc']
        end_grcc_vc = self.decoder.vc['end_grcc']

        # naming: (d: decoder, e: encoder, u: upsample), (o: output, i:input)
        do = vconv.GridRange((0, 100000), (0, w), 1)
        di = vconv.input_range(beg_grcc_vc, end_grcc_vc, do)
        ei = vconv.input_range(mfcc_vc, end_grcc_vc, do)
        mi = vconv.input_range(mfcc_vc.child, end_grcc_vc, do)
        eo = vconv.output_range(mfcc_vc, end_enc_vc, ei)
        uo = vconv.output_range(mfcc_vc, end_ups_vc, ei)

        # Needed for trimming various tensors
        self.enc_in_len = ei.sub_length()
        self.enc_in_mel_len = mi.sub_length()
        # used by jitter_index
        self.embed_len = eo.sub_length() 

        # sets size for wav_dec_in
        self.dec_in_len = di.sub_length()

        # trims wav_enc_input to wav_dec_input
        self.trim_dec_in = torch.tensor([di.sub[0] - ei.sub[0], di.sub[1] -
            ei.sub[0]], dtype=torch.long)

        # needed by wavenet to trim upsampled local conditioning tensor
        self.decoder.trim_ups_out = torch.tensor([di.sub[0] - uo.sub[0],
            di.sub[1] - uo.sub[0]], dtype=torch.long)

        # 
        self.trim_dec_out = torch.tensor(
                [do.sub[0] - di.sub[0], do.sub[1] - di.sub[0]],
                dtype=torch.long)

    def print_geometry(self):
        """
        Print the convolutional geometry
        """
        vc = self.encoder.vc['beg'].parent
        while vc is not None:
            print(vc)
            vc = vc.child


    def __getstate__(self):
        state = { 
                'init_args': self.init_args,
                # 'state_dict': self.state_dict()
                }
        return state 

    def __setstate__(self, state):
        self.init_args = state['init_args']
        self._initialize()
        # self.load_state_dict(state['state_dict'])


    def init_codebook(self, data_source, n_samples):
        """
        Initialize the VQ Embedding with samples from the encoder
        """
        if self.bn_type not in ('vqvae', 'vqvae-ema'):
            raise RuntimeError('init_vq_embed only applies to the vqvae model type')

        bn = self.bottleneck
        e = 0
        n_codes = bn.emb.shape[0]
        k = bn.emb.shape[1]
        samples = np.empty((n_samples, k), dtype=np.float) 
        
        with torch.no_grad():
            while e != n_samples:
                vbatch = next(data_source)
                encoding = self.encoder(vbatch.mel_enc_input)
                ze = self.bottleneck.linear(encoding)
                ze = ze.permute(0, 2, 1).flatten(0, 1)
                c = min(n_samples - e, ze.shape[0])
                samples[e:e + c,:] = ze.cpu()[0:c,:]
                e += c

        km, __ = kmeans(samples, n_codes)
        bn.emb[...] = torch.from_numpy(km)

        if self.bn_type == 'vqvae-ema':
            bn.ema_numer = bn.emb * bn.ema_gamma_comp
            bn.ema_denom = bn.n_sum_ones * bn.ema_gamma_comp
        
    def checksum(self):
        """Return checksum of entire set of model parameters"""
        return util.tensor_digest(self.parameters())
        

    def forward(self, mels, wav_onehot_dec, voice_inds, jitter_index):
        """
        B: n_batch
        M: n_mels
        T: receptive field of autoencoder
        T': receptive field of decoder 
        R: size of local conditioning output of encoder (T - encoder.vc.total())
        N: n_win (# consecutive samples processed in one batch channel)
        Q: n_quant
        mels: (B, M, T)
        wav_compand: (B, T)
        wav_onehot_dec: (B, T')  
        Outputs: 
        quant_pred (B, Q, N) # predicted wav amplitudes
        """
        encoding = self.encoder(mels)
        self.encoding_bn = self.bottleneck(encoding)
        quant = self.decoder(wav_onehot_dec, self.encoding_bn, voice_inds,
                jitter_index)
        return quant

    def run(self, vbatch):
        """
        Run the model on one batch, returning the predicted and
        actual output
        B, T, Q: n_batch, n_timesteps, n_quant
        Outputs:
        quant_pred: (B, Q, T) (the prediction from the model)
        wav_batch_out: (B, T) (the actual data from the same timesteps)
        """
        wav_onehot_dec = self.preprocess(vbatch.wav_dec_input)
        # grad = torch.autograd.grad(wav_onehot_dec, vbatch.wav_dec_input).data

        # Slice each wav input
        trim = self.trim_dec_out
        wav_batch_out = vbatch.wav_dec_input[:,trim[0]:trim[1]]
        # wav_batch_out = torch.take(vbatch.wav_dec_input, vbatch.loss_wav_slice)
        #for b, (sl_b, sl_e) in enumerate(vbatch.loss_wav_slice):
        #    wav_batch_out[b] = vbatch.wav_dec_input[b,sl_b:sl_e]

        # self.wav_batch_out = wav_batch_out
        self.wav_onehot_dec = wav_onehot_dec

        quant = self.forward(vbatch.mel_enc_input, wav_onehot_dec,
                vbatch.voice_index, vbatch.jitter_index)

        pred, target = quant[...,:-1], wav_batch_out[...,1:]

        loss = self.objective(pred, target)
        ag_inputs = (vbatch.mel_enc_input, self.encoding_bn)
        mel_grad, bn_grad = torch.autograd.grad(loss, ag_inputs, retain_graph=True)
        self.objective.metrics.update({
            'mel_grad_sd': mel_grad.std(),
            'bn_grad_sd': bn_grad.std()
            })
        # loss.backward(create_graph=True, retain_graph=True)
        return pred, target, loss 

