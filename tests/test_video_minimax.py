import json
import unittest
from pathlib import Path

from zhipu_agent.stages import video_comfyui as vc

WF = Path(__file__).resolve().parents[1] / "workflow" / "video_minimax_h3_r2v.json"


def _info():
    """按 ComfyUI /object_info 的格式手写的最小登记（只覆盖该工作流用到的节点）。"""
    def node(req=None, opt=None):
        return {"input": {"required": req or {}, "optional": opt or {}}}
    seed = ["INT", {"control_after_generate": True}]
    return {
        "SaveVideo": node({"video": ["VIDEO", {}], "filename_prefix": ["STRING", {}], "format": [["auto", "mp4"], {}], "codec": [["auto", "h264"], {}]}),
        "ResolutionSelector": node({"aspect_ratio": [["16:9 (Widescreen)"], {}], "megapixels": ["FLOAT", {}], "multiple": ["INT", {}]}),
        "VAELoader": node({"vae_name": [["a.safetensors"], {}]}),
        "VAEDecodeAudio": node({"samples": ["LATENT", {}], "vae": ["VAE", {}]}),
        "VAEDecode": node({"samples": ["LATENT", {}], "vae": ["VAE", {}]}),
        "KSamplerSelect": node({"sampler_name": [["res_multistep"], {}]}),
        "BasicScheduler": node({"model": ["MODEL", {}], "scheduler": [["simple"], {}], "steps": ["INT", {}], "denoise": ["FLOAT", {}]}),
        "SamplerCustomAdvanced": node({"noise": ["NOISE", {}], "guider": ["GUIDER", {}], "sampler": ["SAMPLER", {}], "sigmas": ["SIGMAS", {}], "latent_image": ["LATENT", {}]}),
        "BasicGuider": node({"model": ["MODEL", {}], "conditioning": ["CONDITIONING", {}]}),
        "UNETLoader": node({"unet_name": [["x.safetensors"], {}], "weight_dtype": [["default"], {}]}),
        "CLIPLoader": node({"clip_name": [["x"], {}], "type": [["minimax"], {}], "device": [["default"], {}]}),
        "RandomNoise": node({"noise_seed": seed}),
        "CreateVideo": node({"images": ["IMAGE", {}], "fps": ["FLOAT", {}]}, {"audio": ["AUDIO", {}]}),
        "ComfyMathExpression": node({"expression": ["STRING", {}], "values": ["COMFY_AUTOGROW", {}]}),
        "PrimitiveFloat": node({"value": ["FLOAT", {}]}),
        "PrimitiveInt": node({"value": ["INT", {"control_after_generate": True}]}),
        "PrimitiveBoolean": node({"value": ["BOOLEAN", {}]}),
        "PrimitiveStringMultiline": node({"value": ["STRING", {}]}),
        "MiniMaxH3ReferenceToVideo": node(
            {"clip": ["CLIP", {}], "vae": ["VAE", {}], "audio_vae": ["VAE", {}], "prompt": ["STRING", {}],
             "width": ["INT", {}], "height": ["INT", {}], "length": ["INT", {}], "ref_image_size": [["match", "max"], {}]},
            {"ref_images": ["COMFY_AUTOGROW", {}]}),
        "LoadImage": node({"image": [["a.png"], {"image_upload": True}]}),
        "ComfySwitchNode": node({"on_false": ["*", {}], "on_true": ["*", {}], "switch": ["BOOLEAN", {}]}),
        "LoraLoaderModelOnly": node({"model": ["MODEL", {}], "lora_name": [["l"], {}], "strength_model": ["FLOAT", {}]}),
    }


class MiniMaxTest(unittest.TestCase):
    def setUp(self):
        self.tpl = vc.load_workflow(WF, _info())

    def test_ui_converts_and_skips_notes(self):
        self.assertNotIn("116", self.tpl)  # MarkdownNote
        self.assertTrue(vc.is_minimax_r2v(self.tpl))
        gen = self.tpl["136"]["inputs"]
        self.assertEqual(gen["width"], ["115", 0])
        self.assertEqual(gen["ref_image_size"], "match")
        self.assertEqual(self.tpl["129"]["inputs"]["noise_seed"], 261662374822964)  # 种子后面的 'randomize' 被跳过
        self.assertEqual(self.tpl["137"]["inputs"]["image"], "red_superboy_on_city_roof.png")  # 上传按钮的额外值被跳过
        self.assertEqual(self.tpl["131"]["inputs"]["values.a"], ["132", 0])
        self.assertEqual(self.tpl["124"]["inputs"]["scheduler"], "simple")

    def test_build_fills_everything(self):
        wf = vc.build_minimax_r2v(self.tpl, prompt="Use <Picture 1> and <Picture 2>.", seconds=6.2, aspect="16:9",
                                  size_preset="480p", uploaded=["a.png", "b.png", "c.png"], seed=7, prefix="shuohao/E01-01-C01")
        gen = wf["136"]["inputs"]
        refs = sorted(k for k in gen if k.startswith("ref_images."))
        self.assertEqual(refs, ["ref_images.ref_image_0", "ref_images.ref_image_1", "ref_images.ref_image_2"])
        self.assertEqual([wf[gen[k][0]]["inputs"]["image"] for k in refs], ["a.png", "b.png", "c.png"])
        self.assertNotIn("137", wf)  # 模板示例图已移除
        self.assertEqual(wf["138"]["inputs"]["value"], "Use <Picture 1> and <Picture 2>.")
        self.assertEqual(wf["132"]["inputs"]["value"], 7)  # 6.2 秒向上取整
        self.assertEqual((gen["width"], gen["height"]), (864, 480))
        self.assertNotIn("115", wf)
        self.assertEqual(wf["129"]["inputs"]["noise_seed"], 7)
        self.assertEqual(wf["92"]["inputs"]["filename_prefix"], "shuohao/E01-01-C01")
        self.assertFalse(wf["146"]["inputs"]["value"])

    def test_portrait_dims_and_limits(self):
        self.assertEqual(vc.dims_for("9:16", "480p"), (480, 864))
        self.assertEqual(vc.dims_for("16:9", "720p"), (1312, 736))
        self.assertIsNone(vc.dims_for(None, "480p"))
        with self.assertRaises(vc.ComfyError):
            vc.build_minimax_r2v(self.tpl, prompt="x", seconds=5, aspect=None, size_preset=None,
                                 uploaded=[f"{i}.png" for i in range(10)], seed=1, prefix="p")

    def test_frames_formula_matches_template(self):
        self.assertEqual(vc.minimax_frames(5), 5 * 24 + (5 - (120 % 17)) % 17)


if __name__ == "__main__":
    unittest.main()
