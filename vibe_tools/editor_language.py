"""Per-editor response language, reapplied to every model request."""

INSTRUCTIONS = {
    "zh-CN": "所有面向用户的解释、最终回答、测试描述、审查理由及摘要均使用简体中文。",
    "en": "Write all user-facing explanations, final answers, test descriptions, review reasons and summaries in English.",
    "auto": "Use the language of the user's original request for explanations, final answers, test descriptions, review reasons and summaries.",
}


class LanguageProvider:
    def __init__(self, provider, language="zh-CN"):
        self.provider = provider
        self.language = language

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def messages(self, messages):
        instruction = INSTRUCTIONS[self.language] + " Keep code, paths, API names and JSON schema keys unchanged. Translation tasks may include the requested target-language text."
        result = [dict(message) for message in messages]
        systems = [m for m in result if m.get("role") == "system" and isinstance(m.get("content"), str)]
        if systems:
            for message in systems:
                message["content"] += "\n\nResponse language setting (applies to this request): " + instruction
        else:
            result.insert(0, {"role": "system", "content": instruction})
        return result

    def invoke(self, messages, **kwargs):
        return self.provider.invoke(self.messages(messages), **kwargs)

    def invoke_with_tools(self, messages, tools=None):
        return self.provider.invoke_with_tools(self.messages(messages), tools)
