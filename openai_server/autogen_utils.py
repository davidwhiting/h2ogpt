import logging
import os
import re
import subprocess
import sys
import typing
from hashlib import md5
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Union
from types import SimpleNamespace
import uuid

from autogen.code_utils import PYTHON_VARIANTS, WIN32, _cmd, TIMEOUT_MSG
from autogen.coding import LocalCommandLineCodeExecutor, CodeBlock
from autogen.coding.base import CommandLineCodeResult
from autogen import ConversableAgent
from autogen import GroupChatManager
import backoff

from autogen.coding.func_with_reqs import (
    FunctionWithRequirements,
    FunctionWithRequirementsStr,
)
from autogen.coding.utils import silence_pip, _get_file_name_from_content

from typing_extensions import ParamSpec

A = ParamSpec("A")

from openai_server.autogen_streaming import iostream_generator
from openai_server.backend_utils import convert_gen_kwargs
from openai_server.agent_utils import in_pycharm, set_python_path

verbose = os.getenv('VERBOSE', '0').lower() == '1'

danger_mark = 'Potentially dangerous operation detected'
bad_output_mark = 'Output contains sensitive information'


class H2OLocalCommandLineCodeExecutor(LocalCommandLineCodeExecutor):
    def __init__(
        self,
        timeout: int = 60,
        virtual_env_context: Optional[SimpleNamespace] = None,
        work_dir: Union[Path, str] = Path("."),
        functions: List[Union[FunctionWithRequirements[Any, A], Callable[..., Any], FunctionWithRequirementsStr]] = [],
        functions_module: str = "functions",
        execution_policies: Optional[Dict[str, bool]] = None,
        autogen_code_restrictions_level: int = 2,
        stream_output: bool = True,
    ):
        super().__init__(timeout, virtual_env_context, work_dir, functions, functions_module, execution_policies)
        self.autogen_code_restrictions_level = autogen_code_restrictions_level
        self.stream_output = stream_output

    @staticmethod
    def remove_comments_strings(code: str, lang: str) -> str:
        if verbose:
            print(f"Original code:\n{code}", file=sys.stderr)

        if lang in ["bash", "shell", "sh"]:
            # Remove single-line comments
            code = re.sub(r'#.*$', '', code, flags=re.MULTILINE)
            # Remove string literals (this is a simplification and might not catch all cases)
            code = re.sub(r'"[^"]*"', '', code)
            code = re.sub(r"'[^']*'", '', code)
        elif lang == "python":
            # Remove single-line comments
            code = re.sub(r'#.*$', '', code, flags=re.MULTILINE)
            # Remove multi-line strings and docstrings
            code = re.sub(r'"{3}[\s\S]*?"{3}', '', code)
            code = re.sub(r"'{3}[\s\S]*?'{3}", '', code)
            # Remove string literals (this is a simplification and might not catch all cases)
            code = re.sub(r'"[^"]*"', '', code)
            code = re.sub(r"'[^']*'", '', code)

        cleaned_code = code.strip()  # Added strip() to remove leading/trailing whitespace
        if verbose:
            print(f"Cleaned code:\n{cleaned_code}", file=sys.stderr)
        return cleaned_code

    @staticmethod
    def sanitize_command(lang: str, code: str) -> None:
        shell_patterns: typing.Dict[str, str] = {
            r"\brm\b": "Deleting files or directories is not allowed.",
            r"\brm\s+-rf\b": "Use of 'rm -rf' command is not allowed.",
            r"\bmv\b.*?/dev/null": "Moving files to /dev/null is not allowed.",
            r"\bdd\b": "Use of 'dd' command is not allowed.",
            r">\s*/dev/sd[a-z][1-9]?": "Overwriting disk blocks directly is not allowed.",
            r":\(\)\{.*?\}:": "Fork bombs are not allowed.",
            r"\bsudo\b": "Use of 'sudo' command is not allowed.",
            r"\bsu\b": "Use of 'su' command is not allowed.",
            r"\bchmod\b": "Changing file permissions is not allowed.",
            r"\bchown\b": "Changing file ownership is not allowed.",
            r"\bnc\b.*?-e": "Use of netcat in command execution mode is not allowed.",
            r"\bcurl\b.*?\|\s*bash": "Piping curl output to bash is not allowed.",
            r"\bwget\b.*?\|\s*bash": "Piping wget output to bash is not allowed.",
            r"\b(systemctl|service)\s+(start|stop|restart)": "Starting, stopping, or restarting services is not allowed.",
            r"\bnohup\b": "Use of 'nohup' command is not allowed.",
            r"&\s*$": "Running commands in the background is not allowed.",
            r"\bkill\b": "Use of 'kill' command is not allowed.",
            r"\bpkill\b": "Use of 'pkill' command is not allowed.",
            r"\b(python|python3|php|node|ruby)\s+-m\s+http\.server": "Starting an HTTP server is not allowed.",
            r"\biptables\b": "Modifying firewall rules is not allowed.",
            r"\bufw\b": "Modifying firewall rules is not allowed.",
            r"\bexport\b": "Exporting environment variables is not allowed.",
            r"\benv\b": "Accessing or modifying environment variables is not allowed.",
            r"\becho\b.*?>\s*/etc/": "Writing to system configuration files is not allowed.",
            r"\bsed\b.*?-i": "In-place file editing with sed is not allowed.",
            r"\bawk\b.*?-i": "In-place file editing with awk is not allowed.",
            r"\bcrontab\b": "Modifying cron jobs is not allowed.",
            r"\bat\b": "Scheduling tasks with 'at' is not allowed.",
            r"\b(shutdown|reboot|init\s+6|telinit\s+6)\b": "System shutdown or reboot commands are not allowed.",
            r"\b(apt-get|yum|dnf|pacman)\b": "Use of package managers is not allowed.",
            r"\$\(.*?\)": "Command substitution is not allowed.",
            r"`.*?`": "Command substitution is not allowed.",
        }

        python_patterns: typing.Dict[str, str] = {
            # Deleting files or directories
            r"\bos\.(remove|unlink|rmdir)\s*\(": "Deleting files or directories is not allowed.",
            r"\bshutil\.rmtree\s*\(": "Deleting directory trees is not allowed.",

            # System and subprocess usage
            r"\bos\.system\s*\(": "Use of os.system() is not allowed.",
            r"\bsubprocess\.(run|Popen|call|check_output)\s*\(": "Use of subprocess module is not allowed.",

            # Dangerous functions
            r"\bexec\s*\(": "Use of exec() is not allowed.",
            r"\beval\s*\(": "Use of eval() is not allowed.",
            r"\b__import__\s*\(": "Use of __import__() is not allowed.",

            # Import and usage of specific modules
            r"\bimport\s+smtplib\b": "Importing smtplib (for sending emails) is not allowed.",
            r"\bfrom\s+smtplib\s+import\b": "Importing from smtplib (for sending emails) is not allowed.",

            r"\bimport\s+ctypes\b": "Importing ctypes module is not allowed.",
            r"\bfrom\s+ctypes\b": "Importing ctypes module is not allowed.",
            r"\bctypes\.\w+": "Use of ctypes module is not allowed.",

            r"\bimport\s+pty\b": "Importing pty module is not allowed.",
            r"\bpty\.\w+": "Use of pty module is not allowed.",

            r"\bplatform\.\w+": "Use of platform module is not allowed.",

            # Exiting and process management
            r"\bsys\.exit\s*\(": "Use of sys.exit() is not allowed.",
            r"\bos\.chmod\s*\(": "Changing file permissions is not allowed.",
            r"\bos\.chown\s*\(": "Changing file ownership is not allowed.",
            r"\bos\.setuid\s*\(": "Changing process UID is not allowed.",
            r"\bos\.setgid\s*\(": "Changing process GID is not allowed.",
            r"\bos\.fork\s*\(": "Forking processes is not allowed.",

            # Scheduler, debugger, pickle, and marshall usage
            r"\bsched\.\w+": "Use of sched module (for scheduling) is not allowed.",
            r"\bcommands\.\w+": "Use of commands module is not allowed.",
            r"\bpdb\.\w+": "Use of pdb (debugger) is not allowed.",
            r"\bpickle\.loads\s*\(": "Use of pickle.loads() is not allowed.",
            r"\bmarshall\.loads\s*\(": "Use of marshall.loads() is not allowed.",

            # HTTP server usage
            r"\bhttp\.server\b": "Running HTTP servers is not allowed.",
        }

        patterns = shell_patterns if lang in ["bash", "shell", "sh"] else python_patterns
        combined_pattern = "|".join(f"(?P<pat{i}>{pat})" for i, pat in enumerate(patterns.keys()))
        combined_pattern = re.compile(combined_pattern, re.MULTILINE | re.IGNORECASE)

        # Remove comments and strings before checking patterns
        cleaned_code = H2OLocalCommandLineCodeExecutor.remove_comments_strings(code, lang)

        match = re.search(combined_pattern, cleaned_code)
        if match:
            for i, pattern in enumerate(patterns.keys()):
                if match.group(f"pat{i}"):
                    raise ValueError(f"{danger_mark}: {patterns[pattern]}\n\n{cleaned_code}")

    def __execute_code_dont_check_setup(self, code_blocks: List[CodeBlock]) -> CommandLineCodeResult:
        # nearly identical to parent, but with control over guardrails via self.sanitize_command
        logs_all = ""
        file_names = []
        exitcode = -2
        for code_block in code_blocks:
            lang, code = code_block.language, code_block.code
            lang = lang.lower()

            if self.autogen_code_restrictions_level >= 2:
                self.sanitize_command(lang, code)
            elif self.autogen_code_restrictions_level == 1:
                LocalCommandLineCodeExecutor.sanitize_command(lang, code)
            code = silence_pip(code, lang)

            if lang in PYTHON_VARIANTS:
                lang = "python"

            if WIN32 and lang in ["sh", "shell"]:
                lang = "ps1"

            if lang not in self.SUPPORTED_LANGUAGES:
                # In case the language is not supported, we return an error message.
                exitcode = 1
                logs_all += "\n" + f"unknown language {lang}"
                break

            execute_code = self.execution_policies.get(lang, False)
            try:
                # Check if there is a filename comment
                filename = _get_file_name_from_content(code, self._work_dir)
            except ValueError:
                return CommandLineCodeResult(exit_code=1, output="Filename is not in the workspace")

            if filename is None:
                # create a file with an automatically generated name
                code_hash = md5(code.encode()).hexdigest()
                filename = f"tmp_code_{code_hash}.{'py' if lang.startswith('python') else lang}"
            written_file = (self._work_dir / filename).resolve()
            with written_file.open("w", encoding="utf-8") as f:
                f.write(code)
            file_names.append(written_file)

            if not execute_code:
                # Just return a message that the file is saved.
                logs_all += f"Code saved to {str(written_file)}\n"
                exitcode = 0
                continue

            program = _cmd(lang)
            cmd = [program, str(written_file.absolute())]
            env = os.environ.copy()

            if self._virtual_env_context:
                virtual_env_abs_path = os.path.abspath(self._virtual_env_context.bin_path)
                path_with_virtualenv = rf"{virtual_env_abs_path}{os.pathsep}{env['PATH']}"
                env["PATH"] = path_with_virtualenv
                if WIN32:
                    activation_script = os.path.join(virtual_env_abs_path, "activate.bat")
                    cmd = [activation_script, "&&", *cmd]

            try:
                if self.stream_output:
                    from src.utils import execute_cmd_stream
                    exec_func = execute_cmd_stream
                else:
                    exec_func = subprocess.run
                from autogen.io import IOStream
                iostream = IOStream.get_default()
                result = exec_func(
                    cmd, cwd=self._work_dir, capture_output=True, text=True, timeout=float(self._timeout), env=env,
                    print_func=iostream.print,
                )
                iostream.print("\n\n**Completed execution of code blocks.**\n\nENDOFTURN\n\n")
            except subprocess.TimeoutExpired:
                logs_all += "\n" + TIMEOUT_MSG
                # Same exit code as the timeout command on linux.
                exitcode = 124
                break

            logs_all += result.stderr
            logs_all += result.stdout
            exitcode = result.returncode

            if exitcode != 0:
                break

        code_file = str(file_names[0]) if len(file_names) > 0 else None
        return CommandLineCodeResult(exit_code=exitcode, output=logs_all, code_file=code_file)

    def _execute_code_dont_check_setup(self, code_blocks: List[CodeBlock]) -> CommandLineCodeResult:
        try:
            # skip code blocks with # execution: false
            code_blocks = [x for x in code_blocks if '# execution: false' not in x.code]
            # give chance for LLM to give generic code blocks without any execution false
            code_blocks = [x for x in code_blocks if '# execution:' in x.code]

            # ensure no plots pop-up if in pycharm mode or outside docker
            for code_block in code_blocks:
                lang, code = code_block.language, code_block.code
                if lang == 'python':
                    code_block.code = """import matplotlib
matplotlib.use('Agg')  # Set the backend to non-interactive
import matplotlib.pyplot as plt
plt.ioff()
import os
os.environ['TERM'] = 'dumb'
""" + code_block.code

            ret = self.__execute_code_dont_check_setup(code_blocks)

            if ret.exit_code == -2 and len(code_blocks) > 0:
                ret = CommandLineCodeResult(exit_code=0,
                                             output='Code block present, but no code executed (execution tag was false or not present for all code blocks).  This is expected if you had code blocks but they were not meant for python or shell execution.  For example, you may have shown code for demonstration purposes.  If this is expected, then move on normally without concern.')
        except Exception as e:
            if danger_mark in str(e):
                print(f"Code Danger Error: {e}\n\n{code_blocks}", file=sys.stderr)
                # dont' fail, just return the error so LLM can adjust
                ret = CommandLineCodeResult(exit_code=1, output=str(e))
            else:
                raise
        try:
            ret = self.output_guardrail(ret)
        except Exception as e:
            if bad_output_mark in str(e):
                print(f"Code Output Danger Error: {e}\n\n{code_blocks}\n\n{ret}", file=sys.stderr)
                # dont' fail, just return the error so LLM can adjust
                ret = CommandLineCodeResult(exit_code=1, output=str(e))
            else:
                raise
        ret = self.truncate_output(ret)
        return ret

    @staticmethod
    def output_guardrail(ret: CommandLineCodeResult) -> CommandLineCodeResult:
        # List of API key environment variable names to check
        api_key_names = ['OPENAI_AZURE_KEY', 'TWILIO_AUTH_TOKEN', 'NEWS_API_KEY', 'OPENAI_API_KEY_JON',
                         'H2OGPT_H2OGPT_KEY', 'TWITTER_API_KEY', 'FACEBOOK_ACCESS_TOKEN', 'API_KEY', 'LINKEDIN_API_KEY',
                         'STRIPE_API_KEY', 'ADMIN_PASS', 'S2_API_KEY', 'ANTHROPIC_API_KEY', 'AUTH_TOKEN',
                         'AWS_SERVER_PUBLIC_KEY', 'OPENAI_API_KEY', 'HUGGING_FACE_HUB_TOKEN', 'AWS_ACCESS_KEY_ID',
                         'SERPAPI_API_KEY', 'WOLFRAM_ALPHA_APPID', 'AWS_SECRET_ACCESS_KEY', 'ACCESS_TOKEN',
                         'SLACK_API_TOKEN', 'MISTRAL_API_KEY', 'TOGETHERAI_API_TOKEN', 'GITHUB_TOKEN', 'SECRET_KEY',
                         'GOOGLE_API_KEY', 'REPLICATE_API_TOKEN', 'GOOGLE_CLIENT_SECRET', 'GROQ_API_KEY',
                         'AWS_SERVER_SECRET_KEY', 'H2OGPT_OPENAI_BASE_URL', 'H2OGPT_OPENAI_API_KEY',
                         'H2OGPT_MAIN_KWARGS', 'GRADIO_H2OGPT_H2OGPT_KEY']

        # Get the values of these environment variables
        set_api_key_names = set(api_key_names)
        api_key_dict = {key: os.getenv(key, '') for key in set_api_key_names if os.getenv(key, '')}
        set_api_key_values = set(list(api_key_dict.values()))

        # Expanded set of allowed (dummy) values
        set_allowed = {
            '', 'EMPTY', 'DUMMY', 'null', 'NULL', 'Null',
            'YOUR_API_KEY', 'YOUR-API-KEY', 'your-api-key', 'your_api_key',
            'ENTER_YOUR_API_KEY_HERE', 'INSERT_API_KEY_HERE',
            'API_KEY_GOES_HERE', 'REPLACE_WITH_YOUR_API_KEY',
            'PLACEHOLDER', 'EXAMPLE_KEY', 'TEST_KEY', 'SAMPLE_KEY',
            'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            '0000000000000000000000000000000000000000',
            '1111111111111111111111111111111111111111',
            'abcdefghijklmnopqrstuvwxyz123456',
            '123456789abcdefghijklmnopqrstuvwxyz',
            'sk_test_', 'pk_test_',  # Common prefixes for test keys
            'MY_SECRET_KEY', 'MY_API_KEY', 'MY_AUTH_TOKEN',
            'CHANGE_ME', 'REPLACE_ME', 'YOUR_TOKEN_HERE',
            'N/A', 'NA', 'None', 'not_set', 'NOT_SET', 'NOT-SET',
            'undefined', 'UNDEFINED', 'foo', 'bar',
            # Add any other common dummy values you've encountered
        }
        set_allowed = {x.lower() for x in set_allowed}

        # Filter out allowed (dummy) values
        api_key_values = [value.lower() for value in set_api_key_values if value and value.lower() not in set_allowed]

        if ret.output:
            # try to remove offending lines first, if only 1-2 lines, then maybe logging and not code itself
            lines = []
            for line in ret.output.split('\n'):
                if any(api_key_value in line.lower() for api_key_value in api_key_values):
                    print(f"Sensitive information found in output, so removed it: {line}")
                    # e.g. H2OGPT_OPENAI_BASE_URL can appear from logging events from httpx
                    continue
                else:
                    lines.append(line)
            ret.output = '\n'.join(lines)

            # Check if any API key value is in the output and collect all violations
            violated_keys = []
            violated_values = []
            api_key_dict_reversed = {v: k for k, v in api_key_dict.items()}
            for api_key_value in api_key_values:
                if api_key_value in ret.output.lower():
                    # Find the corresponding key name(s) for the violated value
                    violated_key = api_key_dict_reversed[api_key_value]
                    violated_keys.append(violated_key)
                    violated_values.append(api_key_value)

            # If any violations were found, raise an error with all violated keys
            if violated_keys:
                error_message = f"Output contains sensitive information. Violated keys: {', '.join(violated_keys)}"
                print(error_message)
                print("\nBad Output:\n", ret.output)
                print(
                    f"Output contains sensitive information. Violated keys: {', '.join(violated_keys)}\n Violated values: {', '.join(violated_values)}")
                raise ValueError(error_message)

        return ret

    @staticmethod
    def truncate_output(ret: CommandLineCodeResult) -> CommandLineCodeResult:
        if ret.exit_code == 1:
            # then failure, truncated more
            max_output_length = 2048  # about 512 tokens
        else:
            max_output_length = 10000  # about 2500 tokens

        # can't be sure if need head or tail more in general, so split in half
        head_length = max_output_length // 2

        if len(ret.output) > max_output_length:
            trunc_message = f"\n\n...\n\n"
            tail_length = max_output_length - head_length - len(trunc_message)
            head_part = ret.output[:head_length]
            headless_part = ret.output[head_length:]
            tail_part = headless_part[-tail_length:]
            truncated_output = (
                    head_part +
                    trunc_message +
                    tail_part
            )
            ret.output = truncated_output

        return ret


error_patterns = [
    r"Rate limit reached",
    r"Connection timeout",
    r"Server unavailable",
    r"Internal server error",
    r"incomplete chunked read",
]

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backoff")


def backoff_handler(details):
    logger.info(
        f"Backing off {details['wait']:0.1f} seconds after {details['tries']} tries. Exception: {details['exception']}")


class H2OConversableAgent(ConversableAgent):
    @backoff.on_exception(backoff.expo,
                          Exception,
                          max_tries=5,
                          giveup=lambda e: not any(re.search(pattern, str(e)) for pattern in error_patterns),
                          on_backoff=backoff_handler)
    def _generate_oai_reply_from_client(self, llm_client, messages, cache) -> typing.Union[str, typing.Dict, None]:
        try:
            return super()._generate_oai_reply_from_client(llm_client, messages, cache)
        except Exception as e:
            if any(re.search(pattern, str(e)) for pattern in error_patterns):
                logger.info(f"Encountered retryable error: {str(e)}")
                raise  # Re-raise the exception to trigger backoff
            else:
                logger.error(f"Encountered non-retryable error: {str(e)}")
                raise  # If it doesn't match our patterns, raise the original exception


class H2OGroupChatManager(GroupChatManager):
    @backoff.on_exception(backoff.expo,
                          Exception,
                          max_tries=5,
                          giveup=lambda e: not any(re.search(pattern, str(e)) for pattern in error_patterns),
                          on_backoff=backoff_handler)
    def _generate_oai_reply_from_client(self, llm_client, messages, cache) -> typing.Union[str, typing.Dict, None]:
        try:
            return super()._generate_oai_reply_from_client(llm_client, messages, cache)
        except Exception as e:
            if any(re.search(pattern, str(e)) for pattern in error_patterns):
                logger.info(f"Encountered retryable error: {str(e)}")
                raise  # Re-raise the exception to trigger backoff
            else:
                logger.error(f"Encountered non-retryable error: {str(e)}")
                raise  # If it doesn't match our patterns, raise the original exception

def terminate_message_func(msg):
    # in conversable agent, roles are flipped relative to actual OpenAI, so can't filter by assistant
    #        isinstance(msg.get('role'), str) and
    #        msg.get('role') == 'assistant' and

    has_message = isinstance(msg, dict) and isinstance(msg.get('content', ''), str)
    has_term = has_message and msg.get('content', '').endswith("TERMINATE") or msg.get('content', '') == ''
    has_execute = has_message and '# execution: true' in msg.get('content', '')

    if has_execute:
        # sometimes model stops without verifying results if it dumped all steps in one turn
        # force it to continue
        return False

    no_stop_if_code = False
    if no_stop_if_code:
        # don't let LLM stop early if it generated code in last message, so it doesn't try to conclude itself
        from autogen.coding import MarkdownCodeExtractor
        code_blocks = MarkdownCodeExtractor().extract_code_blocks(msg.get("content", ''))
        has_code = len(code_blocks) > 0

        # end on TERMINATE or empty message
        if has_code and has_term:
            print("Model tried to terminate with code present: %s" % len(code_blocks), file=sys.stderr)
            # fix
            msg['content'].replace('TERMINATE', '')
            return False
    if has_term:
        return True
    return False


def get_autogen_response(func=None, use_process=False, **kwargs):
    # raise ValueError("Testing Error Handling 1")  # works

    gen_kwargs = convert_gen_kwargs(kwargs)
    kwargs = gen_kwargs.copy()
    assert func is not None, "func must be provided"
    gen = iostream_generator(func, use_process=use_process, **kwargs)

    ret_dict = {}
    try:
        while True:
            res = next(gen)
            yield res
    except StopIteration as e:
        ret_dict = e.value
    return ret_dict


def get_code_executor(
        autogen_run_code_in_docker,
        autogen_timeout,
        agent_system_site_packages,
        autogen_code_restrictions_level,
        agent_venv_dir,
        temp_dir
        ):
    if autogen_run_code_in_docker:
        from autogen.coding import DockerCommandLineCodeExecutor
        # Create a Docker command line code executor.
        executor = DockerCommandLineCodeExecutor(
            image="python:3.10-slim-bullseye",
            timeout=autogen_timeout,  # Timeout for each code execution in seconds.
            work_dir=temp_dir,  # Use the temporary directory to store the code files.
        )
    else:
        set_python_path()
        from autogen.code_utils import create_virtual_env
        if agent_venv_dir is None:
            username = str(uuid.uuid4())
            agent_venv_dir = ".venv_%s" % username
        env_args = dict(system_site_packages=agent_system_site_packages,
                        with_pip=True,
                        symlinks=True)
        if not in_pycharm():
            virtual_env_context = create_virtual_env(agent_venv_dir, **env_args)
        else:
            print("in PyCharm, can't use virtualenv, so we use the system python", file=sys.stderr)
            virtual_env_context = None
        # work_dir = ".workdir_%s" % username
        # PythonLoader(name='code', ))

        # Create a local command line code executor.
        if autogen_code_restrictions_level >= 2:
            from autogen_utils import H2OLocalCommandLineCodeExecutor
        else:
            from autogen.coding.local_commandline_code_executor import \
                LocalCommandLineCodeExecutor as H2OLocalCommandLineCodeExecutor
        executor = H2OLocalCommandLineCodeExecutor(
            timeout=autogen_timeout,  # Timeout for each code execution in seconds.
            virtual_env_context=virtual_env_context,
            work_dir=temp_dir,  # Use the temporary directory to store the code files.
        )
    return executor

def merge_group_chat_messages(a, b):
    """
    Helps to merge chat messages from two different sources.
    Mostly messages from Group Chat Managers.
    """
    # Create a copy of b to avoid modifying the original list
    merged_list = b.copy()

    # Convert b into a set of contents for faster lookup
    b_contents = {item['content'] for item in b}

    # Iterate through the list a
    for i, item_a in enumerate(a):
        content_a = item_a['content']

        # If the content is not in b, insert it at the correct position
        if content_a not in b_contents:
            # Find the position in b where this content should be inserted
            # Insert right after the content of the previous item in list a (if it exists)
            if i > 0:
                prev_content = a[i - 1]['content']
                # Find the index of the previous content in the merged list
                for j, item_b in enumerate(merged_list):
                    if item_b['content'] == prev_content:
                        merged_list.insert(j + 1, item_a)
                        break
            else:
                # If it's the first item in a, just append it to the beginning
                merged_list.insert(0, item_a)

            # Update the b_contents set
            b_contents.add(content_a)

    return merged_list