from __future__ import annotations

import json
import unittest

from companion_v2.adapters.astrbot.native import persona_instruction
from companion_v2.execution.models import TaskError


class PersonaExampleTests(unittest.TestCase):
    def test_host_processed_examples_may_carry_private_markers(self):
        # AstrBot 4.29 会为每条示例附 ``_no_save``；旧实现要求恰好只有
        # role/content，于是把正常人格判成"格式无法读取"，4.29.1 上保存映射失败。
        persona = {"prompt": "你是小凌。", "_begin_dialogs_processed": [
            {"role": "user", "content": "看什么番呀", "_no_save": True},
            {"role": "assistant", "content": "哥哥看过吗", "_no_save": True},
        ]}
        text = persona_instruction(persona)
        self.assertTrue(text.startswith("你是小凌。"))
        examples = json.loads(text.split("\n", 3)[-1])
        self.assertEqual(examples, [{"role": "user", "content": "看什么番呀"},
                                    {"role": "assistant", "content": "哥哥看过吗"}])
        self.assertNotIn("_no_save", text)

    def test_examples_still_require_role_and_text(self):
        for broken in ([{"role": "user"}],
                       [{"role": "tool", "content": "x"}],
                       [{"role": "user", "content": ["x"]}],
                       [{"role": "user", "content": "x", "extra": 1}][:0] + ["raw"]):
            with self.assertRaises(TaskError):
                persona_instruction({"prompt": "p", "_begin_dialogs_processed": broken})

    def test_host_preset_system_guidance_is_retained_with_the_examples(self):
        dialogs = [{"role": "system", "content": "Use assistant examples for style.", "_no_save": True},
                   {"role": "user", "content": "Question", "_no_save": True},
                   {"role": "assistant", "content": "Answer", "_no_save": True}]
        text = persona_instruction({"prompt": "Native persona", "_begin_dialogs_processed": dialogs})
        examples = json.loads(text.split("\n", 3)[-1])
        self.assertEqual(examples, [{"role": d["role"], "content": d["content"]} for d in dialogs])

    def test_plain_prompt_without_examples_is_unchanged(self):
        self.assertEqual(persona_instruction({"prompt": "只有提示词。"}), "只有提示词。")

    def test_missing_prompt_is_still_an_error(self):
        with self.assertRaises(TaskError):
            persona_instruction({"prompt": None})


if __name__ == "__main__":
    unittest.main()
