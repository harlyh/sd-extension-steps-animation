import json
import os
import shutil
import string
import pathlib


import gradio as gr
from modules import scripts
from modules.images import save_image
try:
    from modules.sd_samplers_kdiffusion import KDiffusionSampler
    from modules.sd_samplers_common import sample_to_image
except ImportError:
    from modules.sd_samplers import KDiffusionSampler, sample_to_image

# configurable section
video_rate = 30
author = 'https://github.com/vladmandic'
cli_template = "ffmpeg -hide_banner -loglevel {loglevel} -hwaccel auto -y -framerate {framerate} -i \"{inpath}/%5d.jpg\" -r {videorate} {preset} {minterpolate} {flags} -metadata title=\"{description}\" -metadata description=\"{info}\" -metadata author=\"stable-diffusion\" -metadata album_artist=\"{author}\" \"{outfile}\"" # note: <https://wiki.multimedia.cx/index.php/FFmpeg_Metadata>

presets = {
    'x264': '-vcodec libx264 -preset medium -crf 23',
    'x265': '-vcodec libx265 -preset faster -crf 28',
    'vpx-vp9': '-vcodec libvpx-vp9 -crf 34 -b:v 0 -deadline realtime -cpu-used 4',
    'aom-av1': '-vcodec libaom-av1 -crf 28 -b:v 0 -usage realtime -cpu-used 8 -pix_fmt yuv444p',
    'prores_ks': '-vcodec prores_ks -profile:v 3 -vendor apl0 -bits_per_mb 8000 -pix_fmt yuv422p10le',
}


# internal state variables
current_step = 0
orig_callback_state = KDiffusionSampler.callback_state


def safestring(text: str):
    lines = []
    for line in text.splitlines():
        lines.append(line.translate(str.maketrans('', '', string.punctuation)).strip())
    res = ', '.join(lines)
    return res[:1000]


class Script(scripts.Script):
    # script title to show in ui
    def title(self):
        return 'Steps animation'


    # is ui visible: process/postprocess triggers for always-visible scripts otherwise use run as entry point
    def show(self, is_img2img):
        return scripts.AlwaysVisible


    # ui components
    def ui(self, is_visible):
        with gr.Accordion('Steps animation', open = False, elem_id='steps-animation'):
            gr.HTML("""
                <a href="https://github.com/vladmandic/generative-art/tree/main/extensions">
                Creates animation sequence from denoised intermediate steps with video frame interpolation to achieve desired animation duration</a><br>""")
            with gr.Row():
                is_enabled = gr.Checkbox(label = 'Script Enabled', value = False)
                codec = gr.Radio(label = 'Codec', choices = ['x264', 'x265', 'vpx-vp9', 'aom-av1', 'prores_ks'], value = 'x264')
                interpolation = gr.Radio(label = 'Interpolation', choices = ['none', 'mci', 'blend'], value = 'mci')
            with gr.Row():
                duration = gr.Slider(label = 'Duration', minimum = 0.5, maximum = 120, step = 0.1, value = 10)
                skip_steps = gr.Slider(label = 'Skip steps', minimum = 0, maximum = 100, step = 1, value = 0)
            with gr.Row():
                debug = gr.Checkbox(label = 'Debug info', value = False)
                run_incomplete = gr.Checkbox(label = 'Run on incomplete', value = True)
                tmp_delete = gr.Checkbox(label = 'Delete intermediate', value = True)
                out_create = gr.Checkbox(label = 'Create animation', value = True)
            with gr.Row():
                tmp_path = gr.Textbox(label = 'Intermediate files path', lines = 1, value = 'intermediate')
                out_path = gr.Textbox(label = 'Output animation path', lines = 1, value = 'animation')

        return [is_enabled, codec, interpolation, duration, skip_steps, debug, run_incomplete, tmp_delete, out_create, tmp_path, out_path]


    # runs on each step for always-visible scripts
    def process(self, p, is_enabled, codec, interpolation, duration, skip_steps, debug, run_incomplete, tmp_delete, out_create, tmp_path, out_path):
        if is_enabled:
            def callback_state(self, d):
                global current_step
                current_step = int(d['i']) + 1
                if (skip_steps == 0) or (current_step > skip_steps):
                    try:
                        image = sample_to_image(samples = d['denoised'], index = 0)
                        inpath = os.path.join(p.outpath_samples, tmp_path)
                        save_image(image, inpath, '', extension = 'jpg', short_filename = True, no_prompt = True) # filename using 00000 format so its easier for ffmpeg sequence parsing
                    except Exception as e:
                        print('Steps animation error: save intermediate image', e)
                    if debug:
                        print(f'Steps animation saving interim image from step {current_step}')

                return orig_callback_state(self, d)

            setattr(KDiffusionSampler, 'callback_state', callback_state)


    # run at the end of sequence for always-visible scripts
    def postprocess(self, p, processed, is_enabled, codec, interpolation, duration, skip_steps, debug, run_incomplete, tmp_delete, out_create, tmp_path, out_path):
        global current_step
        setattr(KDiffusionSampler, 'callback_state', orig_callback_state)
        if not is_enabled:
            return
        # callback happened too early, it happens with large number of steps and some samplers or if interrupted
        if vars(processed)['steps'] != current_step and current_step > 0:
            print('Steps animation warning: postprocess early call', { 'current': current_step, 'target': vars(processed)['steps'] })
            if not run_incomplete:
                return
        if current_step == 0:
            print('Save animation error: steps is zero, likely using unsupported sampler or interrupted')
            return
        # create dictionary with all input and output parameters
        v = vars(processed)
        params = {
            'prompt': safestring(v['prompt']),
            'negative': safestring(v['negative_prompt']),
            'seed': v['seed'],
            'sampler': v['sampler_name'],
            'cfgscale': v['cfg_scale'],
            'steps': v['steps'],
            'current': current_step,
            'skip': skip_steps,
            'info': safestring(v['info']),
            'model': v['info'].split('Model:')[1].split()[0] if ('Model:' in v['info']) else 'unknown', # parse string if model info is present
            'embedding': v['info'].split('Used embeddings:')[1].split()[0] if ('Used embeddings:' in v['info']) else 'none',  # parse string if embedding info is present
            'faces': v['face_restoration_model'],
            'timestamp': v['job_timestamp'],
            'inpath': os.path.join(p.outpath_samples, tmp_path),
            'outpath': os.path.join(p.outpath_samples, out_path),
            'codec': 'lib' + codec,
            'duration': duration,
            'interpolation': interpolation,
            'loglevel': 'error',
            'cli': cli_template,
            'framerate': max(0, 1.0 * (current_step - skip_steps) / duration),
            'videorate': video_rate,
            'author': author,
            'preset': presets[codec],
            'flags': '-movflags +faststart',
            'ffmpeg': shutil.which('ffmpeg'), # detect if ffmpeg executable is present in path
        }
        # append conditionals to dictionary
        params['minterpolate'] = '' if (params['interpolation'] == 'none') else '-vf minterpolate=mi_mode={mi},fifo'.format(mi = params['interpolation'])
        if params['codec'] == 'libvpx-vp9':
            suffix = '.webm'
        elif params['codec'] == 'libprores_ks':
            suffix = '.mov'
        else:
            suffix = '.mp4'
        params['outfile'] = os.path.join(params['outpath'], str(params['seed']) + '-' + safestring(params['prompt'])[:96] + suffix)
        params['description'] = '{prompt} | negative {negative} | seed {seed} | sampler {sampler} | cfgscale {cfgscale} | steps {steps} | current {current} | model {model} | embedding {embedding} | faces {faces} | timestamp {timestamp} | interpolation {interpolation}'.format(**params)
        current_step = 0 # reset back to zero
        if debug:
            params['loglevel'] = 'info'
            print('Steps animation params:', json.dumps(params, indent = 2))
        if out_create:
            nimg = len(os.listdir(params['inpath']))
            if params['framerate'] == 0:
                print('Save animation error: framerate is zero')
                return
            if nimg == 0:
                print('Save animation no interim images were created')
                return
            if not os.path.isdir(params['outpath']):
                print('Save animation create folder:', params['outpath'])
                pathlib.Path(params['outpath']).mkdir(parents=True, exist_ok=True)
            if not os.path.isdir(params['inpath']) or not os.path.isdir(params['outpath']):
                print('Steps animation error: folder not found', params['inpath'], params['outpath'])
                return
            if params['ffmpeg'] is None:
                print('Steps animation error: ffmpeg not found:')
            else:
                print('Steps animation creating movie sequence:', params['outfile'], 'images:', nimg)
                cmd = params['cli'].format(**params)
                if debug:
                    print('Steps animation CMD:', cmd)
                # actual ffmpeg call
                os.system(cmd)
        if tmp_delete:
            for root, _dirs, files in os.walk(params['inpath']):
                if debug:
                    print('Steps animation removing {n} files from temp folder: {path}'.format(path = root, n = len(files)))
                for file in files:
                    f = os.path.join(root, file)
                    if os.path.isfile(f):
                        os.remove(f)
