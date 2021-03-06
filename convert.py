import argparse
#from stgan2_new.model import Generator
#from stgan.model import Generator
#from model2 import Generator as Generator2
from stgan.model import Generator as Gen

from stgan2_ls.model import Generator as LSGen
from stgan_adain.model import Generator as AdaGen
from stgan_adain.model import GeneratorSplit  as AdaGenSplit
from stgan_adain.model import Generator2D as AdaGen2D
from stgan_adain.model import SPEncoder as SPEncoder
from stgan_adain.model import SPEncoderPool
from stgan_adain.model import SPEncoderPool1D
from stgan_adain_gse.model import Generator as AdaGenGSE
from stgan_adain_gse.model import SPEncoder as SPEncoderGSE
from torch.autograd import Variable
import torch
import torch.nn.functional as F
import numpy as np
import os
from os.path import join, basename, dirname, split, exists
import time
import datetime
from data_loader import to_categorical
import librosa
from utils import *
import glob
import json

from concurrent.futures import ProcessPoolExecutor
import subprocess
from tqdm import tqdm
from functools import partial
#[1006 new feature: add loud norm]
import pyloudnorm
class TestDataset(object):
    """Dataset for testing.
        
        This test dataloader is for one src spk to one trg spk. 
        Src and trg spk can be defined by config or positional parameters. If they are defined by config, it will ignore the positional parameters.
    """
    def __init__(self, config, src_spk = None, trg_spk = None, speakers = None):
        
        if config.src_spk is not None and config.trg_spk is not None:
            assert config.trg_spk in speakers, f"The trg_spk {config.trg_spk} does not exist in speakers {speakers}"
            self.src_spk = config.src_spk
            self.trg_spk = config.trg_spk

        if config.src_spk is not None and config.trg_spk is None:
            raise Exception("config trg spk should be defined")
        if config.trg_spk is not None and config.src_spk is None:
            raise Exception("config src spk should be defined")
        
        if config.src_spk is None and config.trg_spk is None:
            self.src_spk = src_spk
            self.trg_spk = trg_spk
        
        print(f" ==== create test dataloader for src {self.src_spk} and trg {self.trg_spk} ====", flush=True)

        # find source speakers all mc files
        self.mc_files = sorted(glob.glob(join(config.test_data_dir, f'{self.src_spk}*.npy')))
        self.trg_mc_files = sorted(glob.glob(join(config.test_data_dir, f'{self.trg_spk}*.npy')))
        self.src_spk_stats = np.load(join(config.train_data_dir, f'{self.src_spk}_stats.npz'))
        self.src_wav_dir = f'{config.wav_dir}/{self.src_spk}'
        self.trg_wav_dir = f'{config.wav_dir}/{self.trg_spk}'
        
        self.trg_spk_stats = np.load(join(config.train_data_dir, f'{self.trg_spk}_stats.npz'))

        self.logf0s_mean_src = self.src_spk_stats['log_f0s_mean']
        self.logf0s_std_src = self.src_spk_stats['log_f0s_std']
        self.logf0s_mean_trg = self.trg_spk_stats['log_f0s_mean']
        self.logf0s_std_trg = self.trg_spk_stats['log_f0s_std']
        self.mcep_mean_src = self.src_spk_stats['coded_sps_mean']
        self.mcep_std_src = self.src_spk_stats['coded_sps_std']
        self.mcep_mean_trg = self.trg_spk_stats['coded_sps_mean']
        self.mcep_std_trg = self.trg_spk_stats['coded_sps_std']
        
        self.spk_idx = speakers.index(self.trg_spk)
        spk_cat = to_categorical([self.spk_idx], num_classes=len(speakers))
        self.spk_c_trg = spk_cat
        
        self.org_idx = speakers.index(self.src_spk)
        org_cat = to_categorical([self.org_idx], num_classes = len(speakers))
        self.spk_c_org = org_cat


    def get_batch_test_data(self, batch_size=4):
        '''
            if batch_size is not defined through config, it will convert all mc_files for src_spk
        '''
        
        if batch_size is None:
            batch_size = len(self.mc_files)
        batch_data = []
        trg_mcfile = self.trg_mc_files[1]
        for i in range(batch_size):
            mcfile = self.mc_files[i]
            filename = basename(mcfile)
            #if exists( join( self.trg_wav_dir, self.trg_spk + '_' + filename.split('_')[1].replace('npy','wav') ) )   :
            #    refwav_path = join(self.trg_wav_dir, self.trg_spk + '_' +filename.split('_')[1].replace('npy','wav'))
            #else:
            #refwav_path = join(self.trg_wav_dir, os.listdir(self.trg_wav_dir)[0])
            wavfile_path = join(self.src_wav_dir, self.src_spk + '_' + filename.split('_')[1].replace('npy', 'wav'))
            #batch_data.append((wavfile_path, refwav_path))
            batch_data.append((wavfile_path, trg_mcfile))
        return batch_data 


def load_wav(wavfile, sr=16000):
    wav, _ = librosa.load(wavfile, sr=sr, mono=True)
    return wav_padding(wav, sr=sr, frame_period=5, multiple = 4)  # TODO
    # return wav


def process_test_loader(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb, config, sp_enc):
    test_wavfiles = test_loader.get_batch_test_data(batch_size=config.num_converted_wavs)
    test_wavs = [(load_wav(wavfile, sampling_rate), trg_mc_path ) for wavfile, trg_mc_path in test_wavfiles]
    pair_list = []
    #[1006 new feature: add loud norm]
    loud_meter = pyloudnorm.Meter(sampling_rate)
    with torch.no_grad():
        for idx, (wav, trg_mc)  in enumerate(test_wavs):
            print(f'len source wav {len(wav)} trg mc path {trg_mc}', flush=True)

            wav_name = basename(test_wavfiles[idx][0])
            
            # print(wav_name)
            
            #[1006 new feature: add loud norm]
            src_loudness = loud_meter.integrated_loudness(wav)
            # get source speech features
            f0, timeaxis, sp, ap = world_decompose(wav=wav, fs=sampling_rate, frame_period=frame_period)
            f0_converted = pitch_conversion(f0=f0, 
                mean_log_src=test_loader.logf0s_mean_src, std_log_src=test_loader.logf0s_std_src, 
                mean_log_target=test_loader.logf0s_mean_trg, std_log_target=test_loader.logf0s_std_trg)
            coded_sp = world_encode_spectral_envelop(sp=sp, fs=sampling_rate, dim=num_mcep)
            
            print("Before being fed into G: ", coded_sp.shape, flush=True)
            coded_sp_norm = (coded_sp - test_loader.mcep_mean_src) / test_loader.mcep_std_src
            coded_sp_norm_tensor = torch.FloatTensor(coded_sp_norm.T).unsqueeze_(0).unsqueeze_(1).to(device)
            
            trg_spk_cat = torch.FloatTensor(test_loader.spk_c_trg).to(device)
            trg_spk_label = torch.LongTensor([test_loader.spk_idx]).to(device)           
            org_spk_cat = torch.FloatTensor(test_loader.spk_c_org).to(device)
            org_spk_label = torch.LongTensor([test_loader.org_idx]).to(device)           
            
            if sp_enc is not None:
                if not config.use_spk_mean:
                    
                    #_, _, ref_sp, _ = world_decompose(wav = ref_wav, fs = sampling_rate, frame_period = frame_period)
                    #coded_ref_sp = world_encode_spectral_envelop(sp = ref_sp, fs = sampling_rate, dim = num_mcep)
                    #coded_ref_sp_norm = (coded_ref_sp - test_loader.mcep_mean_trg) / test_loader.mcep_std_trg
                    #coded_ref_sp_norm_tensor = torch.FloatTensor(coded_ref_sp_norm.T).unsqueeze_(0).unsqueeze_(1).to(device)
                    coded_ref_sp_norm = np.load(trg_mc)
                    coded_ref_sp_norm_tensor = torch.FloatTensor(coded_ref_sp_norm.T).unsqueeze_(0).unsqueeze_(1).to(device)
                    trg_spk_cond = sp_enc(coded_ref_sp_norm_tensor, trg_spk_label)    
                    src_spk_cond = sp_enc(coded_sp_norm_tensor, org_spk_label )
                else:
                    if test_loader.trg_spk in spk2emb:
                        trg_spk_cond = spk2emb[test_loader.trg_spk]
                        trg_spk_cond = torch.FloatTensor(trg_spk_cond).unsqueeze_(0).to(device)
                        
                        src_spk_cond = spk2emb[test_loader.src_spk]
                        src_spk_cond = torch.FloatTensor(src_spk_cond).unsqueeze_(0).to(device)
                    else:
                        raise Exception(f'trg spk {test_loader.trg_spk} not in spk2emb {spk2emb.keys()}')
            
            if sp_enc is not None:
                coded_sp_converted_norm = G(coded_sp_norm_tensor, src_spk_cond, trg_spk_cond).data.cpu().numpy()
            else:
                coded_sp_converted_norm = G(coded_sp_norm_tensor, org_spk_cat, trg_spk_cat).data.cpu().numpy()


            coded_sp_converted = np.squeeze(coded_sp_converted_norm).T * test_loader.mcep_std_trg + test_loader.mcep_mean_trg
            coded_sp_converted = np.ascontiguousarray(coded_sp_converted)
            
            
            print("After being fed into G: ", coded_sp_converted.shape, flush=True)
            #synthesis to converted wav
            wav_transformed = world_speech_synthesis(f0=f0_converted, coded_sp=coded_sp_converted, 
                                                    ap=ap, fs=sampling_rate, frame_period=frame_period)
            wav_id = wav_name.split('.')[0]
            #[1006 new feature: add loud norm]
            output_loudness = loud_meter.integrated_loudness(wav_transformed)
            if config.use_loudnorm:
                wav_transformed = pyloudnorm.normalize.loudness(wav_transformed, output_loudness, src_loudness)

            cvt_wav_path = f'{wav_id}-{test_loader.src_spk}-vcto-{test_loader.trg_spk}.wav'
            librosa.output.write_wav(join(config.convert_dir, str(config.resume_iters),
                cvt_wav_path), wav_transformed, sampling_rate)
            
            pair_list.append((join(config.convert_dir, str(config.resume_iters), cvt_wav_path), trg_mc))


            
            if config.cpsyn:
                wav_cpsyn = world_speech_synthesis(f0=f0, coded_sp=coded_sp, 
                                                ap=ap, fs=sampling_rate, frame_period=frame_period)
                librosa.output.write_wav(join(config.convert_dir, str(config.resume_iters), f'cpsyn-{wav_name}'), wav_cpsyn, sampling_rate)
    return pair_list


def _convert(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb, config, sp_enc):
                
    pair_list = process_test_loader(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb, config, sp_enc)
    #all_pair_list.extend(pair_list)
    #return all_pair_list
    


def test(config):
    

    #load speakers
    with open(config.speaker_path) as f:
        speakers = json.load(f)
    
    spk2emb = {}
    if config.use_spk_mean and config.generator == 'AdaGen':
        if not os.path.exists(join(config.spk_mean_dir, str(config.resume_iters))):
            raise Exception()
        for spk in speakers:
            if not exists(join(config.spk_mean_dir, str(config.resume_iters),f'{spk}-emd_mean.npy')):
                raise Exception()
            emb = np.load(join(config.spk_mean_dir, str(config.resume_iters),f'{spk}-emd_mean.npy'))
            if spk not in spk2emb:
                spk2emb[spk] = emb
            else:
                raise Exception('speaker embedding overwrite')

    os.makedirs(join(config.convert_dir, str(config.resume_iters)), exist_ok=True)
    sampling_rate, num_mcep, frame_period= config.sample_rate, 36, 5
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    if config.generator.startswith('AdaGen'):
        G = eval(config.generator)(num_speakers = config.num_speakers, aff = config.drop_affine, res_block_name = config.res_block).to(device)
    elif config.generator == 'LSGen' or config.generator == 'Gen':   
        G = eval(config.generator)(num_speakers = config.num_speakers).to(device)
    else:
        raise Exception()
    # Restore model
    print(f'Loading the trained models from step {config.resume_iters}...', flush=True)
    # [0922 new feature]: load in ema model ckpt for evaluation
    if config.use_ema:
        G_path = join(config.model_save_dir, f'{config.resume_iters}-G.ckpt.ema')
    else:
        G_path = join(config.model_save_dir, f'{config.resume_iters}-G.ckpt')
    G.load_state_dict(torch.load(G_path, map_location=lambda storage, loc: storage))
    #G.eval()
    
    if config.generator.startswith('AdaGen'):
        sp_enc = eval(config.spenc)(num_speakers = config.num_speakers,spk_cls = config.spk_cls ).to(device)
        # [0922 new feature]: load in ema model ckpt for evaluation
        if config.use_ema:
            sp_path = join(config.model_save_dir, f'{config.resume_iters}-sp.ckpt.ema')
        else:
            sp_path = join(config.model_save_dir, f'{config.resume_iters}-sp.ckpt')
        sp_enc.load_state_dict(torch.load(sp_path, map_location=lambda storage, loc: storage))
        sp_enc.eval()
    else:
        sp_enc = None
    
    
    all_pair_list = []
    if config.src_spk is not None and config.trg_spk is not None:
        
        test_loader = TestDataset(config, speakers = speakers)
        pair_list = process_test_loader(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb,config, sp_enc)
        #all_pair_list.extend(pair_list)
    else:
        # convert all src_trg pairs len(speakers) * (len(speakers) -1) pairs
        #if config.num_workers is not None:
        #    futures = []
        #    executor = ProcessPoolExecutor(max_workers = config.num_workers)
        for src in speakers[:25]:
            for trg in speakers:
                if src != trg:
                    test_loader = TestDataset(config, src_spk = src, trg_spk = trg, speakers = speakers)
                    #if config.num_workers is None:
                    _convert(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb, config, sp_enc)           
        
                    #else:
                    #    futures.append(
                    #        executor.submit(partial(_convert, test_loader, G, device, sampling_rate, 
                    #            num_mcep, frame_period, None, config,sp_enc 
                    #            ))
                            
                    #    )    
        #if config.num_workers is not None:
        #    result_list = [future.result() for future in tqdm(futures, postfix = '\n')]  
        #            test_loader = TestDataset(config, src_spk = src, trg_spk = trg, speakers = speakers)
        #            pair_list = process_test_loader(test_loader, G, device, sampling_rate, num_mcep, frame_period, spk2emb, config, sp_enc)
         #           all_pair_list.extend(pair_list)
    
    #with open(config.pair_list_path,'w') as f:
    #    for pair in all_pair_list:
    #        f.write(f'{pair[0]} {pair[1]}\n')

    """
    # Read a batch of testdata
    test_wavfiles = test_loader.get_batch_test_data(batch_size=config.num_converted_wavs)
    test_wavs = [(load_wav(wavfile, sampling_rate), ref_wav) for wavfile, ref_wav in test_wavfiles]

    with torch.no_grad():
        for idx, wav in enumerate(test_wavs):
            print(len(wav))
            wav_name = basename(test_wavfiles[idx])
            
            target_ref_wav = config.wav_dir + '/' + config.src_spk + '/' + wav_name + '.wav'

            # print(wav_name)

            # get source speech features
            f0, timeaxis, sp, ap = world_decompose(wav=wav, fs=sampling_rate, frame_period=frame_period)
            f0_converted = pitch_conversion(f0=f0, 
                mean_log_src=test_loader.logf0s_mean_src, std_log_src=test_loader.logf0s_std_src, 
                mean_log_target=test_loader.logf0s_mean_trg, std_log_target=test_loader.logf0s_std_trg)
            coded_sp = world_encode_spectral_envelop(sp=sp, fs=sampling_rate, dim=num_mcep)
            print("Before being fed into G: ", coded_sp.shape)
            coded_sp_norm = (coded_sp - test_loader.mcep_mean_src) / test_loader.mcep_std_src
            coded_sp_norm_tensor = torch.FloatTensor(coded_sp_norm.T).unsqueeze_(0).unsqueeze_(1).to(device)
            spk_conds = torch.FloatTensor(test_loader.spk_c_trg).to(device)
            # print(spk_conds.size())
            coded_sp_converted_norm = G(coded_sp_norm_tensor, spk_conds).data.cpu().numpy()
            coded_sp_converted = np.squeeze(coded_sp_converted_norm).T * test_loader.mcep_std_trg + test_loader.mcep_mean_trg
            coded_sp_converted = np.ascontiguousarray(coded_sp_converted)
            print("After being fed into G: ", coded_sp_converted.shape)
            #synthesis to converted wav
            wav_transformed = world_speech_synthesis(f0=f0_converted, coded_sp=coded_sp_converted, 
                                                    ap=ap, fs=sampling_rate, frame_period=frame_period)
            wav_id = wav_name.split('.')[0]
            librosa.output.write_wav(join(config.convert_dir, str(config.resume_iters),
                f'{wav_id}-vcto-{test_loader.trg_spk}.wav'), wav_transformed, sampling_rate)
            if config.cpsyn:
                wav_cpsyn = world_speech_synthesis(f0=f0, coded_sp=coded_sp, 
                                                ap=ap, fs=sampling_rate, frame_period=frame_period)
                librosa.output.write_wav(join(config.convert_dir, str(config.resume_iters), f'cpsyn-{wav_name}'), wav_cpsyn, sampling_rate)

    """

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Model configuration.
    parser.add_argument('--num_speakers', type=int, default=10, help='dimension of speaker labels')
    parser.add_argument('--sample_rate', type=int, default=16000, help='sample rate')
    parser.add_argument('--num_converted_wavs', type=int, default=None, help='number of wavs to convert, if not defined, will convert all')
    parser.add_argument('--resume_iters', type=int, default=None, help='step to resume for testing.')
    parser.add_argument('--src_spk', type=str, default=None, help = 'target speaker.')
    parser.add_argument('--trg_spk', type=str, default=None, help = 'target speaker.')
    parser.add_argument('--generator', type=str, default='Generator')
    parser.add_argument('--res_block', type=str, default='ResidualBlockSplit')
    parser.add_argument('--spenc', type = str, default = 'SPEncoder')
    parser.add_argument('--spk_cls', default = False, action = 'store_true')
    parser.add_argument('--drop_affine', default = True, action = 'store_false')
    parser.add_argument('--use_ema', default = False, action = 'store_true')
    parser.add_argument('--use_loudnorm', default = False, action = 'store_true')
    # Directories.
    parser.add_argument('--train_data_dir', type=str, default='./data/mc/train')
    parser.add_argument('--test_data_dir', type=str, default='./data/mc/test')
    parser.add_argument('--wav_dir', type=str, default="./data/VCTK-Corpus/wav16")
    parser.add_argument('--log_dir', type=str, default='./logs')
    parser.add_argument('--model_save_dir', type=str, default='./models')
    parser.add_argument('--convert_dir', type=str, default='./converted')
    parser.add_argument('--speaker_path', type = str, required = True)
    parser.add_argument('--pair_list_path', type = str, required = True)

    #options
    parser.add_argument('--cpsyn', default = False, action = 'store_true')
    parser.add_argument('--use_spk_mean', default = False, action = 'store_true', help = 'compute mean of speaker embedding as use it as the input of Generator')
    parser.add_argument('--spk_mean_dir', type = str, help = 'speaker embedding mean vector dir, if use_spk_mean is true')
    
    parser.add_argument('--num_workers', type = int, default = None, help = 'multi-process')
    config = parser.parse_args()
    
    print(config, flush=True)
    if config.resume_iters is None:
        raise RuntimeError("Please specify the step number for resuming.")
    test(config)
