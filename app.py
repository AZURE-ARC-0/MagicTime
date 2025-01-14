import os
import copy
import torch
import random
import gradio as gr
from glob import glob
from omegaconf import OmegaConf
from safetensors import safe_open
from diffusers import AutoencoderKL
from diffusers import EulerDiscreteScheduler, DDIMScheduler
from diffusers.utils.import_utils import is_xformers_available
from transformers import CLIPTextModel, CLIPTokenizer

from utils.unet import UNet3DConditionModel
from utils.pipeline_magictime import MagicTimePipeline
from utils.util import save_videos_grid, convert_ldm_unet_checkpoint, convert_ldm_clip_checkpoint, convert_ldm_vae_checkpoint, load_diffusers_lora_unet, convert_ldm_clip_text_model

pretrained_model_path   = "./ckpts/Base_Model/stable-diffusion-v1-5"
inference_config_path   = "./sample_configs/RealisticVision.yaml"
magic_adapter_s_path    = "./ckpts/Magic_Weights/magic_adapter_s/magic_adapter_s.ckpt"
magic_adapter_t_path    = "./ckpts/Magic_Weights/magic_adapter_t"
magic_text_encoder_path = "./ckpts/Magic_Weights/magic_text_encoder"

css = """
.toolbutton {
    margin-buttom: 0em 0em 0em 0em;
    max-width: 2.5em;
    min-width: 2.5em !important;
    height: 2.5em;
}
"""

examples = [
    # 1-ToonYou
    [
        "ToonYou_beta6.safetensors", 
        "motion_module.ckpt", 
        "Bean sprouts grow and mature from seeds.",
        "worst quality, low quality, letterboxed",
        512, 512, "13204175718326964000"
    ],
    # 2-RCNZ
    [
        "RcnzCartoon.safetensors", 
        "motion_module.ckpt", 
        "Time-lapse of a simple modern house's construction in a Minecraft virtual environment: beginning with an avatar laying a white foundation, progressing through wall erection and interior furnishing, to adding roof and exterior details, and completed with landscaping and a tall chimney.",
        "worst quality, low quality, letterboxed",
        512, 512, "1268480012"
    ],
    # 3-RealisticVision
    [
        "RealisticVisionV60B1_v51VAE.safetensors", 
        "motion_module.ckpt", 
        "Cherry blossoms transitioning from tightly closed buds to a peak state of bloom. The progression moves through stages of bud swelling, petal exposure, and gradual opening, culminating in a full and vibrant display of open blossoms.",
        "worst quality, low quality, letterboxed",
        512, 512, "2038801077"
    ]
]

# clean Grdio cache
print(f"### Cleaning cached examples ...")
os.system(f"rm -rf gradio_cached_examples/")


class MagicTimeController:
    def __init__(self):
        
        # config dirs
        self.basedir                = os.getcwd()
        self.stable_diffusion_dir   = os.path.join(self.basedir, "ckpts", "Base_Model")
        self.motion_module_dir      = os.path.join(self.basedir, "ckpts", "Base_Model", "motion_module")
        self.personalized_model_dir = os.path.join(self.basedir, "ckpts", "DreamBooth")
        self.savedir                = os.path.join(self.basedir, "outputs")
        os.makedirs(self.savedir, exist_ok=True)

        self.dreambooth_list    = []
        self.motion_module_list = []
        
        self.selected_dreambooth    = None
        self.selected_motion_module = None
        
        self.refresh_motion_module()
        self.refresh_personalized_model()
        
        # config models
        self.inference_config      = OmegaConf.load(inference_config_path)[1]

        self.tokenizer             = CLIPTokenizer.from_pretrained(pretrained_model_path, subfolder="tokenizer")
        self.text_encoder          = CLIPTextModel.from_pretrained(pretrained_model_path, subfolder="text_encoder").cuda()
        self.vae                   = AutoencoderKL.from_pretrained(pretrained_model_path, subfolder="vae").cuda()
        self.unet                  = UNet3DConditionModel.from_pretrained_2d(pretrained_model_path, subfolder="unet", unet_additional_kwargs=OmegaConf.to_container(self.inference_config.unet_additional_kwargs)).cuda()
        
        self.text_model = CLIPTextModel.from_pretrained("openai/clip-vit-large-patch14")

        self.update_dreambooth(self.dreambooth_list[0])
        self.update_motion_module(self.motion_module_list[0])

        from swift import Swift
        magic_adapter_s_state_dict = torch.load(magic_adapter_s_path, map_location="cpu")
        self.unet = load_diffusers_lora_unet(self.unet, magic_adapter_s_state_dict, alpha=1.0)
        self.unet = Swift.from_pretrained(self.unet, magic_adapter_t_path)
        self.text_encoder = Swift.from_pretrained(self.text_encoder, magic_text_encoder_path)

        
    def refresh_motion_module(self):
        motion_module_list = glob(os.path.join(self.motion_module_dir, "*.ckpt"))
        self.motion_module_list = [os.path.basename(p) for p in motion_module_list]

    def refresh_personalized_model(self):
        dreambooth_list = glob(os.path.join(self.personalized_model_dir, "*.safetensors"))
        self.dreambooth_list = [os.path.basename(p) for p in dreambooth_list]

    def update_dreambooth(self, dreambooth_dropdown):
        self.selected_dreambooth = dreambooth_dropdown
        
        dreambooth_dropdown = os.path.join(self.personalized_model_dir, dreambooth_dropdown)
        dreambooth_state_dict = {}
        with safe_open(dreambooth_dropdown, framework="pt", device="cpu") as f:
            for key in f.keys(): dreambooth_state_dict[key] = f.get_tensor(key)
                
        converted_vae_checkpoint = convert_ldm_vae_checkpoint(dreambooth_state_dict, self.vae.config)
        self.vae.load_state_dict(converted_vae_checkpoint)

        converted_unet_checkpoint = convert_ldm_unet_checkpoint(dreambooth_state_dict, self.unet.config)
        self.unet.load_state_dict(converted_unet_checkpoint, strict=False)
        
        text_model = copy.deepcopy(self.text_model)
        self.text_encoder = convert_ldm_clip_text_model(text_model, dreambooth_state_dict)
        return gr.Dropdown()

    def update_motion_module(self, motion_module_dropdown):
        self.selected_motion_module = motion_module_dropdown
        motion_module_dropdown = os.path.join(self.motion_module_dir, motion_module_dropdown)
        motion_module_state_dict = torch.load(motion_module_dropdown, map_location="cpu")
        _, unexpected = self.unet.load_state_dict(motion_module_state_dict, strict=False)
        assert len(unexpected) == 0
        return gr.Dropdown()
    
    
    def magictime(
        self,
        dreambooth_dropdown,
        motion_module_dropdown,
        prompt_textbox,
        negative_prompt_textbox,
        width_slider,
        height_slider,
        seed_textbox,
    ):
        if self.selected_dreambooth != dreambooth_dropdown: self.update_dreambooth(dreambooth_dropdown)
        if self.selected_motion_module != motion_module_dropdown: self.update_motion_module(motion_module_dropdown)
        
        if is_xformers_available(): self.unet.enable_xformers_memory_efficient_attention()

        pipeline = MagicTimePipeline(
            vae=self.vae, text_encoder=self.text_encoder, tokenizer=self.tokenizer, unet=self.unet,
            scheduler=DDIMScheduler(**OmegaConf.to_container(self.inference_config.noise_scheduler_kwargs))
        ).to("cuda")
        
        if int(seed_textbox) > 0: seed = int(seed_textbox)
        else: seed = random.randint(1, 1e16)
        torch.manual_seed(int(seed))
        
        assert seed == torch.initial_seed()
        print(f"### seed: {seed}")
        
        generator = torch.Generator(device="cuda")
        generator.manual_seed(seed)
        
        sample = pipeline(
            prompt_textbox,
            negative_prompt     = negative_prompt_textbox,
            num_inference_steps = 25,
            guidance_scale      = 8.,
            width               = width_slider,
            height              = height_slider,
            video_length        = 16,
            generator           = generator,
        ).videos

        save_sample_path = os.path.join(self.savedir, f"sample.mp4")
        save_videos_grid(sample, save_sample_path)
    
        json_config = {
            "prompt": prompt_textbox,
            "n_prompt": negative_prompt_textbox,
            "width": width_slider,
            "height": height_slider,
            "seed": seed,
            "dreambooth": dreambooth_dropdown,
        }
        return gr.Video(value=save_sample_path), gr.Json(value=json_config)
        
controller = MagicTimeController()


def ui():
    with gr.Blocks(css=css) as demo:
        gr.Markdown(
            """
            <h2 align="center"> <a href="https://github.com/PKU-YuanGroup/MagicTime">MagicTime: Time-lapse Video Generation Models as Metamorphic Simulators</a></h2>
            <h5 align="center"> If you like our project, please give us a star ⭐ on GitHub for the latest update.  </h2>

            [GitHub](https://img.shields.io/github/stars/PKU-YuanGroup/MagicTime) | [arXiv](https://arxiv.org/abs/2404.05014) | [Home Page](https://pku-yuangroup.github.io/MagicTime/) | [Dataset](https://drive.google.com/drive/folders/1WsomdkmSp3ql3ImcNsmzFuSQ9Qukuyr8?usp=sharing)
            """
        )
        with gr.Row():
            with gr.Column():
                dreambooth_dropdown     = gr.Dropdown( label="DreamBooth Model", choices=controller.dreambooth_list,    value=controller.dreambooth_list[0],    interactive=True )
                motion_module_dropdown  = gr.Dropdown( label="Motion Module",  choices=controller.motion_module_list, value=controller.motion_module_list[0], interactive=True )

                dreambooth_dropdown.change(fn=controller.update_dreambooth,       inputs=[dreambooth_dropdown],    outputs=[dreambooth_dropdown])
                motion_module_dropdown.change(fn=controller.update_motion_module, inputs=[motion_module_dropdown], outputs=[motion_module_dropdown])

                prompt_textbox          = gr.Textbox( label="Prompt",          lines=3 )
                negative_prompt_textbox = gr.Textbox( label="Negative Prompt", lines=3, value="worst quality, low quality, nsfw, logo")

                with gr.Accordion("Advance", open=False):
                    with gr.Row():
                        width_slider  = gr.Slider(  label="Width",  value=512, minimum=256, maximum=1024, step=64 )
                        height_slider = gr.Slider(  label="Height", value=512, minimum=256, maximum=1024, step=64 )
                    with gr.Row():
                        seed_textbox = gr.Textbox( label="Seed",  value=-1)
                        seed_button  = gr.Button(value="\U0001F3B2", elem_classes="toolbutton")
                        seed_button.click(fn=lambda: gr.Textbox.update(value=random.randint(1, 1e16)), inputs=[], outputs=[seed_textbox])

                generate_button = gr.Button( value="Generate", variant='primary' )

            with gr.Column():
                result_video = gr.Video( label="Generated Animation", interactive=False )
                json_config  = gr.Json( label="Config", value=None )

            inputs  = [dreambooth_dropdown, motion_module_dropdown, prompt_textbox, negative_prompt_textbox, width_slider, height_slider, seed_textbox]
            outputs = [result_video, json_config]
            
            generate_button.click( fn=controller.magictime, inputs=inputs, outputs=outputs )
                
        gr.Examples( fn=controller.magictime, examples=examples, inputs=inputs, outputs=outputs, cache_examples=True )
        
    return demo


if __name__ == "__main__":
    demo = ui()
    demo.queue(max_size=20)
    demo.launch()