from fnmatch import fnmatch
import os
from typing import Iterable, List, Tuple
from argparse import ArgumentParser
from re import search
import openai
from github import Github, PullRequest, Commit, IssueComment

def code_type(filename: str) -> str | None:
    match = search(r"^.*\.([^.]*)$", filename)
    if match:
        match match.group(1):
            case "js":
                return "JavaScript"
            case "ts":
                return "TypeScript"
            case "java":
                return "Java"
            case "py":
                return "Python"
            case "sh":
                return "Bash"


def prompt(filename: str, contents: str) -> str:
    code = "code"
    type = code_type(filename)
    if type:
        code = f"{type} {code}"

    return f"Please evaluate the  {code} below.\n" \
           "Provide answers to the following questions in numbered lists\n" \
           "   - does the code below has obvious bugs?\n" \
           "   - are there any security issues in the code?\n" \
           "   - how do you assess the readability of the code?\n" \
           "   - are there any code duplication in the provided code?\n" \
           "   - are variable names descriptive enough?\n" \
           "   - is the code well documented?\n" \
           "   - can you identify possible performance improvements for this code?\n" \
           "At the end of your answer, please summarize and explain what changes should be performed in the " \
           "provided code to improve its quality\n" \
           "If a question doesn't have a new suggestion omit it from your answer." \
           f"```\n{contents}\n```"


def line_start_patch(patch: str) -> int:
    match = search(r"^@@ [-+](\d*),", patch)
    return int(match.group(1))


def files_for_review(pull: PullRequest, patterns: List[str]) -> Iterable[Tuple[str, int, Commit.Commit]]:
    changes = {}
    commits = pull.get_commits()
    for commit in commits:
        for file in commit.files:
            if file.status in ["unchanged", "removed"]:
                continue
            for pattern in patterns:
                if fnmatch(file.filename, pattern) and not changes.get(file.filename, None):
                    changes[file.filename] = (line_start_patch(file.patch), commit)

    return [(x[0], max(1, x[1][0]), x[1][1]) for x in changes.items()]


def review(filename: str, content: str, model: str, temperature: float, max_tokens: int) -> str:
    chat_review = openai.ChatCompletion.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": prompt(filename, content),
        }]
    ).choices[0].message.content
    return f"*ChatGPT review for {filename}:*\n" \
           f"{chat_review}"

def handle_replies(github_token: str, github_pr_id: int, api_key: str, model: str, temperature: float, max_tokens: int):
    g = Github(github_token)
    repo = g.get_repo(os.getenv("GITHUB_REPOSITORY"))
    pull = repo.get_pull(github_pr_id)
    openai.api_key = api_key

    # Get comments
    comments = pull.get_issue_comments()

    for comment in comments:
        if comment.user.login == "github-actions[bot]":
            # Find replies to the bot's comment
            replies = [reply for reply in comments if reply.reply_to_id == comment.id and reply.user.login != "github-actions[bot]"]

            for reply in replies:
                # Check if the reply is already handled by the bot
                bot_replies = [bot_reply for bot_reply in comments if bot_reply.reply_to_id == reply.id and bot_reply.user.login == "github-actions[bot]"]
                if bot_replies:
                    print(f"Skipped already handled reply from {reply.user.login}: {reply.body}")
                    continue

                # Get the reply content and generate a response
                question = reply.body
                response = openai.Completion.create(
                    engine=model,
                    prompt=f"{question}\n\n{reply.original_commit_id}:\n{reply.path} line {reply.original_position}\n```{reply.diff_hunk}```",
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=1,
                    frequency_penalty=0,
                    presence_penalty=0,
                )

                # Post the response as a comment reply
                reply.create_reply(response.choices[0].text)

                # You can mark the reply as handled by deleting it, adding a label, or any other method.
                # In this case, we'll just print that the reply has been handled.
                print(f"Handled reply from {reply.user.login}: {reply.body}")

        else:
            print(f"Skipped non-bot comment from {comment.user.login}: {comment.body}")


def main():
    parser = ArgumentParser()
    parser.add_argument("--openai_api_key", required=True, help="OpenAI API Key")
    parser.add_argument("--github_token", required=True, help="Github Access Token")
    parser.add_argument("--github_pr_id", required=True, type=int, help="Github PR ID to review")
    parser.add_argument("--openai_model", default="gpt-3.5-turbo",
                        help="GPT-3 model to use. Options: gpt-3.5-turbo, text-davinci-002, "
                             "text-babbage-001, text-curie-001, text-ada-001. Recommended: gpt-3.5-turbo")
    parser.add_argument("--openai_temperature", default=0.5, type=float,
                        help="Sampling temperature to use, a float [0, 1]. Higher values "
                             "mean the model will take more risks. Recommended: 0.5")
    parser.add_argument("--openai_max_tokens", default=2048, type=int,
                        help="The maximum number of tokens to generate in the completion.")
    parser.add_argument("--files", help="Comma separated list of UNIX file patterns to target for review")
    args = parser.parse_args()

    file_patterns = args.files.split(",")
    openai.api_key = args.openai_api_key
    g = Github(args.github_token)

    repo = g.get_repo(os.getenv("GITHUB_REPOSITORY"))
    pull = repo.get_pull(args.github_pr_id)
    comments = []
    for filename, line, commit in files_for_review(pull, file_patterns):
        content = repo.get_contents(filename, commit.sha).decoded_content.decode("utf8")
        if len(content) == 0:
            continue
        comments.append({"path": filename,
                         "line": line,
                         "body": review(filename, content, args.openai_model, args.openai_temperature,
                                        args.openai_max_tokens)})

    if len(comments) > 0:
        handle_replies(args.github_token, args.github_pr_id, args.openai_api_key, args.openai_model, args.openai_temperature, args.openai_max_tokens)
        pull.create_review(body="**ChatGPT code review**", event="COMMENT", comments=comments)

if __name__ == "__main__":
    main()
