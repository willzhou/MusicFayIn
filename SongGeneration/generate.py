import sys
import os

import time
import json
import torch
import torchaudio
import numpy as np
from omegaconf import OmegaConf

from codeclm.trainer.codec_song_pl import CodecLM_PL
from codeclm.models import CodecLM
from third_party.demucs.models.pretrained import get_model_from_yaml

auto_prompt_type = ['Pop', 'R&B', 'Dance', 'Jazz', 'Folk', 'Rock', 'Chinese Style', 'Chinese Tradition', 'Metal', 'Reggae', 'Chinese Opera', 'Auto']

class Separator:
    def __init__(self, dm_model_path='third_party/demucs/ckpt/htdemucs.pth', dm_config_path='third_party/demucs/ckpt/htdemucs.yaml', gpu_id=0) -> None:
        if torch.cuda.is_available() and gpu_id < torch.cuda.device_count():
            self.device = torch.device(f"cuda:{gpu_id}")
        else:
            self.device = torch.device("cpu")
        self.demucs_model = self.init_demucs_model(dm_model_path, dm_config_path)

    def init_demucs_model(self, model_path, config_path):
        model = get_model_from_yaml(config_path, model_path)
        model.to(self.device)
        model.eval()
        return model
    
    def load_audio(self, f):
        a, fs = torchaudio.load(f)
        if (fs != 48000):
            a = torchaudio.functional.resample(a, fs, 48000)
        if a.shape[-1] >= 48000*10:
            a = a[..., :48000*10]
        else:
            a = torch.cat([a, a], -1)
        return a[:, 0:48000*10]
    
    def run(self, audio_path, output_dir='tmp', ext=".flac"):
        os.makedirs(output_dir, exist_ok=True)
        name, _ = os.path.splitext(os.path.split(audio_path)[-1])
        output_paths = []

        for stem in self.demucs_model.sources:
            output_path = os.path.join(output_dir, f"{name}_{stem}{ext}")
            if os.path.exists(output_path):
                output_paths.append(output_path)
        if len(output_paths) == 1:  # 4
            vocal_path = output_paths[0]
        else:
            drums_path, bass_path, other_path, vocal_path = self.demucs_model.separate(audio_path, output_dir, device=self.device)
            for path in [drums_path, bass_path, other_path]:
                os.remove(path)
        full_audio = self.load_audio(audio_path)
        vocal_audio = self.load_audio(vocal_path)
        bgm_audio = full_audio - vocal_audio
        return full_audio, vocal_audio, bgm_audio



if __name__ == "__main__":
    torch.backends.cudnn.enabled = False
    OmegaConf.register_new_resolver("eval", lambda x: eval(x))
    OmegaConf.register_new_resolver("concat", lambda *x: [xxx for xx in x for xxx in xx])
    OmegaConf.register_new_resolver("get_fname", lambda: os.path.splitext(os.path.basename(sys.argv[1]))[0])
    OmegaConf.register_new_resolver("load_yaml", lambda x: list(OmegaConf.load(x)))
    np.random.seed(int(time.time()))    
    ckpt_path = sys.argv[1]
    input_jsonl = sys.argv[2]
    save_dir = sys.argv[3]
    cfg_path = os.path.join(ckpt_path, 'config.yaml')
    ckpt_path = os.path.join(ckpt_path, 'model.pt')
    cfg = OmegaConf.load(cfg_path)
    cfg.mode = 'inference'
    max_duration = cfg.max_dur
    
    # Define model or load pretrained model
    model_light = CodecLM_PL(cfg, ckpt_path)

    model_light = model_light.eval().cuda()
    model_light.audiolm.cfg = cfg
    model = CodecLM(name = "tmp",
        lm = model_light.audiolm,
        audiotokenizer = model_light.audio_tokenizer,
        max_duration = max_duration,
        seperate_tokenizer = model_light.seperate_tokenizer,
    )
    separator = Separator()
    auto_prompt = torch.load('ckpt/prompt.pt')
    merge_prompt = [item for sublist in auto_prompt.values() for item in sublist]
    cfg_coef = 1.5 #25
    temp = 0.9
    top_k = 50
    top_p = 0.0
    record_tokens = True
    record_window = 50

    model.set_generation_params(duration=max_duration, extend_stride=5, temperature=temp, cfg_coef=cfg_coef,
                                top_k=top_k, top_p=top_p, record_tokens=record_tokens, record_window=record_window)
    os.makedirs(save_dir, exist_ok=True)
    os.makedirs(save_dir + "/audios", exist_ok=True)
    os.makedirs(save_dir + "/jsonl", exist_ok=True)

    with open(input_jsonl, "r") as fp:
        lines = fp.readlines()

    new_items = []
    for line in lines:
        item = json.loads(line)
        target_wav_name = f"{save_dir}/audios/{item['idx']}.flac"
        lyric = item["gt_lyric"]
        descriptions = item["descriptions"] if "descriptions" in item else None
        # get prompt audio
        if "prompt_audio_path" in item:
            assert os.path.exists(item['prompt_audio_path']), f"prompt_audio_path {item['prompt_audio_path']} not found"
            assert 'auto_prompt_audio_type' not in item, f"auto_prompt_audio_type and prompt_audio_path cannot be used together"
            pmt_wav, vocal_wav, bgm_wav = separator.run(item['prompt_audio_path'])
            melody_is_wav = True
        elif "auto_prompt_audio_type" in item:
            assert item["auto_prompt_audio_type"] in auto_prompt_type, f"auto_prompt_audio_type {item['auto_prompt_audio_type']} not found"
            if item["auto_prompt_audio_type"] == "Auto": 
                prompt_token = merge_prompt[np.random.randint(0, len(merge_prompt))]
            else:
                prompt_token = auto_prompt[item["auto_prompt_audio_type"]][np.random.randint(0, len(auto_prompt[item["auto_prompt_audio_type"]]))]
            pmt_wav = prompt_token[:,[0],:]
            vocal_wav = prompt_token[:,[1],:]
            bgm_wav = prompt_token[:,[2],:]
            melody_is_wav = False
        else:
            pmt_wav = None
            vocal_wav = None
            bgm_wav = None
            melody_is_wav = True
            
        generate_inp = {
            'lyrics': [lyric.replace("  ", " ")],
            'descriptions': [descriptions],
            'melody_wavs': pmt_wav,
            'vocal_wavs': vocal_wav,
            'bgm_wavs': bgm_wav,
            'melody_is_wav': melody_is_wav,
        }
        start_time = time.time()
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            tokens = model.generate(**generate_inp, return_tokens=True)
        mid_time = time.time()
            
        with torch.no_grad():
            if melody_is_wav:   
                wav_seperate = model.generate_audio(tokens, pmt_wav, vocal_wav, bgm_wav)
            else:
                wav_seperate = model.generate_audio(tokens)
        end_time = time.time()
        torchaudio.save(target_wav_name, wav_seperate[0].cpu().float(), cfg.sample_rate)
        print(f"process{item['idx']}, lm cost {mid_time - start_time}s, diffusion cost {end_time - mid_time}")

        item["idx"] = f"{item['idx']}"
        item["wav_path"] = target_wav_name
        new_items.append(item)
    
    src_jsonl_name = os.path.split(input_jsonl)[-1]
    with open(f"{save_dir}/jsonl/{src_jsonl_name}.jsonl", "w", encoding='utf-8') as fw:
        for item in new_items:
            fw.writelines(json.dumps(item, ensure_ascii=False)+"\n")
