import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["ZHIPU_API_KEY"],
    base_url="https://open.bigmodel.cn/api/paas/v4/",
)

MODEL = os.environ["ZHIPU_MODEL"]


def chat(messages):
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.2,
    )
    return response.choices[0].message.content