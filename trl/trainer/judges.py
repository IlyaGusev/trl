import os
import random
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List

from accelerate import Accelerator
from huggingface_hub import InferenceClient

from ..import_utils import is_llmblender_available, is_openai_available


if is_llmblender_available():
    import llm_blender

if is_openai_available():
    from openai import BadRequestError, OpenAI


DEFAULT_SYSTEM_PROMPT = """The most important part of the task is choosing which of the Assistant's responses is more helpful and honest. Please consider the responses carefully, and use your overall judgment. Ask yourself "did this response represent a thoughtful, honest, and reasonable attempt to either directly help me, or to converse with me to elicit information that will make it easier for the Assistant to help later on in this conversation". Note that this is subtle - if the context is about a difficult personal situation, a helpful response might involve showing empathy and understanding. But if the context is about something practical, a helpful response may be a concise response, and not one that prolongs the interaction with irrelevant questions and comments. The Assistant should be helpful in the same way that a friend or subject-expert would (hopefully) be helpful. Note response containing "Human:" and/or  Assistant:" that tries to extend the conversation should be considered as not helpful and honest. You are an expert rater of helpful and honest Assistant responses. Given the context and the two responses choose the most helpful and honest response based on the definitions above.
Context - {prompt}
Response 1 - {response1}
Response 2 - {response2}
Preferred Response="""


class BaseJudge(ABC):
    """Base class for local LLM judges."""

    def shuffle_pairs(self, pairs: List[List[str]]) -> List[List[str]]:
        """Shuffles each pair of completions to mitigate positional bias."""
        shuffled_pairs = []
        for pair in pairs:
            shuffled_pair = pair.copy()
            random.shuffle(shuffled_pair)
            shuffled_pairs.append(shuffled_pair)
        return shuffled_pairs

    @abstractmethod
    def judge_single(self, prompt: str, completion_pair: List[str]) -> int:
        """Judge a single completion pair."""
        raise NotImplementedError("Judge subclasses must implement this method.")

    def judge_batch(self, prompts: List[str], completion_pairs: List[List[str]]) -> List[int]:
        """Judge a batch of completion pairs."""
        results = []
        completion_pairs = self.shuffle_pairs(completion_pairs)
        for prompt, completion_pair in zip(prompts, completion_pairs):
            result = self.judge_single(prompt, completion_pair)
            results.append(result)
        return results


class BaseAPIJudge:
    """Base class for LLM judges reached via an API."""

    # TODO: add max_requests parameter to limit the number of requests made
    def __init__(self, system_prompt: str = None, max_tries: int = 5, max_workers: int = 8):
        if system_prompt is None:
            system_prompt = DEFAULT_SYSTEM_PROMPT
        self.system_prompt = system_prompt
        self.max_tries = max_tries
        self.thread_pool_executor = ThreadPoolExecutor(max_workers=max_workers)

    def __del__(self) -> None:
        self.thread_pool_executor.shutdown()

    def get_reply(self, content: str) -> str:
        raise NotImplementedError

    def judge(self, prompt: str, completion_pair: List[str], shuffle_order: bool, max_tokens: int = 3) -> int:
        if self.max_tries == 0:
            print("Max retries reached")
            return random.choice([0, 1])

        shuffle_index = 0 if not shuffle_order else random.choice([0, 1])

        content = self.system_prompt.format(
            prompt=prompt, response1=completion_pair[shuffle_index], response2=completion_pair[1 - shuffle_index]
        )
        reply = self.get_reply(content)
        reply = reply.strip()

        # First answer
        if reply in [
            "1",
            "Option 1",
            "Summary 1",
            "Response 1",
            "The first response",
            "Answer 1",
        ]:
            return shuffle_index
        # Second answer
        elif reply in [
            "2",
            "Option 2",
            "Summary 2",
            "Response 2",
            "The second response",
            "Answer 2",
        ]:
            return 1 - shuffle_index
        # Ties
        elif reply in [
            "Both responses are",
            "Both Response",
            "Neither response",
            "Neither",
            "Neither response is",
            "Neither response addresses",
            "Neither response addresses",
            "Neither response correctly",
            "Both responses provided",
            "Both responses provide",
            "Neither response provides",
            "The two responses",
            "Both responses here",
        ]:
            return random.choice([0, 1])
        # Unknown reply
        else:
            print("Error: ", reply)
            self.max_tries -= 1
            return self.judge(prompt, completion_pair, max_tokens, shuffle_order)

    def judge_single(self, prompt: str, completion_pair: List[str], shuffle_order: bool = True) -> Future:
        return self.thread_pool_executor.submit(self.judge, prompt, completion_pair, shuffle_order)

    def judge_batch(
        self, prompts: List[str], completion_pairs: List[List[str]], shuffle_order: bool = True
    ) -> List[int]:
        futures = []
        for prompt, completion_pair in zip(prompts, completion_pairs):
            future = self.judge_single(prompt, completion_pair, shuffle_order=shuffle_order)
            futures.append(future)

        results = [f.result() for f in futures]

        return results


class HuggingFaceJudge(BaseAPIJudge):
    def __init__(self, max_workers=8, model="meta-llama/Meta-Llama-3-70B-Instruct"):
        super().__init__(max_workers=max_workers)
        self.client = InferenceClient(model=model)
        self.model_name = model

    def get_reply(self, content: str) -> str:
        try:
            response = self.client.chat_completion(
                messages=[{"role": "user", "content": content}],
                max_tokens=50,
                stop=["<|eot_id|>"],  # For llama-3 models
            )
            reply = response.choices[0].message.content
            return reply
        except BadRequestError as e:
            print("BadRequestError", e)
            print("Content: ", content)
            return random.choice(["0", "1"])


class OpenAIJudge(BaseAPIJudge):
    def __init__(self, max_workers=8, model_name="gpt-4-turbo-preview"):
        if not is_openai_available():
            raise ValueError("OpenAI client is not installed. Please install it with 'pip install openai'.")
        super().__init__(max_workers=max_workers)
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=5)
        self.model_name = model_name

    def get_reply(self, content: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": content}],
                max_tokens=3,
            )
            reply = response.choices[0].message.content
            return reply
        except BadRequestError as e:
            print("BadRequestError", e)
            print("Content: ", content)
            return random.choice(["0", "1"])


class PairRMJudge(BaseJudge):
    """LLM judge based on the PairRM model from AllenAI.

    See: https://huggingface.co/llm-blender/PairRM
    """

    def __init__(self):
        if not is_llmblender_available():
            raise ValueError("llm-blender is not installed. Please install it with 'pip install llm-blender'.")
        self.blender = llm_blender.Blender()
        self.blender.loadranker("llm-blender/PairRM", device=Accelerator().device)

    def judge_single(self, prompt: str, completion_pair: List[str]) -> int:
        ranks = self.blender.rank([prompt], [completion_pair])
        # PairRM is 1-indexed, so we subtract 1 to make it 0-indexed
        ranks -= 1
        return ranks[0][0]


class MockJudge(BaseJudge):
    def judge_single(self, prompt: str, completion_pair: List[str]) -> int:
        return random.choice([0, 1])
