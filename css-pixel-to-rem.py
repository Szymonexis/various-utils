from functools import reduce
import os
from pprint import pprint
import re


REGEX = r'((\d+\.)?\d+)(px)'
REM_SIZE = 16


class BCOLORS:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def get_directory_path() -> str:
    correct = False
    directory_path = ""

    while not correct:
        directory_path = input("Enter directory path (needs to be absolute): ")

        if not os.path.isabs(directory_path):
            print(
                f"{BCOLORS.FAIL}The provided path is not absolute. Please try again.{BCOLORS.ENDC}")
            continue

        if not os.path.exists(directory_path):
            print(
                f"{BCOLORS.FAIL}The provided path does not exist. Please try again.{BCOLORS.ENDC}")
            continue

        if not os.path.isdir(directory_path):
            print(
                f"{BCOLORS.FAIL}The provided path is not a directory. Please try again.{BCOLORS.ENDC}")
            continue

        if not os.access(directory_path, os.W_OK):
            print(
                f"{BCOLORS.FAIL}The provided path is not writable. Please try again.{BCOLORS.ENDC}")
            continue

        message = f"This directory will be targeted: {directory_path}"
        correct_str = input(f"{message}\nIs this correct? (y/N): ")
        correct = correct_str.lower() == 'y'

    return directory_path


def get_file_extensions() -> list[str]:
    correct = False
    extensions: list[str] = []

    while not correct:
        extensions_str = input(
            "Enter file extensions separated by comma-space (f.e. \"ts, scss, json, component.ts\"): ")
        extensions = list(map(lambda x: f".{x}", extensions_str.split(', ')))

        message = f"These extensions will be targeted:\n{
            reduce(lambda acc, x: f'{acc}{x}',
                   map(lambda x: f'- *{x}\n', extensions),
                   "")}"

        correct_str = input(f"{message}Is this correct? (y/N): ")
        correct = correct_str.lower() == 'y'

    return extensions


def grab_files(directory_path: str, extensions: list[str]) -> list[str]:
    matched_files = []
    for root, _, files in os.walk(directory_path):
        for file in files:
            for ext in extensions:
                if file.endswith(ext):
                    matched_files.append(os.path.join(root, file))

    return matched_files


def get_matches_in_file(file: str, context_length=5) -> list[list[str]]:
    with open(file, 'r') as f:
        lines = f.readlines()

        contexts: list[list[str]] = []

        for i, line in enumerate(lines):
            line_matches = re.search(REGEX, line) is not None
            if line_matches:
                start = max(0, i - context_length)
                end = min(len(lines), i + context_length)
                context = lines[start:end]
                context[context_length] = f"{BCOLORS.GREEN}{context[context_length]}{BCOLORS.ENDC}"
                contexts.append(context)

        return contexts


def ask_to_replace_with_context(file: str, match_contexts: list[list[str]]) -> bool:
    os.system('cls' if os.name == 'nt' else 'clear')
    print("Matched contexts:")

    for i, match_context in enumerate(match_contexts):
        print("---")
        for context_line in match_context:
            print(context_line, end='')

        print("---")
        if i != len(match_contexts) - 1:
            print()

    correct = False
    correct_str = input(
        f"Would you like to replace these matches for file \"{file}\" (from px to rem units)? (y/N): ")
    correct = correct_str.lower() == 'y'
    return correct


def replace_pixels_with_rem(file: str) -> None:
    with open(file, 'r') as rf:
        content = rf.read()

        def replace_with_rem(match):
            pixels = float(match.group(1))
            rems = round(pixels / REM_SIZE, 3)
            return f"{rems}rem"

        new_content = re.sub(REGEX, replace_with_rem, content)

        with open(file, 'w') as wf:
            wf.write(new_content)


def main() -> None:
    directory_path = get_directory_path()
    extensions = get_file_extensions()
    files = grab_files(directory_path, extensions)

    for file in files:
        contexts = get_matches_in_file(file)

        if len(contexts) == 0:
            continue

        result = ask_to_replace_with_context(file, contexts)

        if result:
            replace_pixels_with_rem(file)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nProcess interrupted. Exiting gracefully. Goodbye!")
