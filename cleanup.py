import os
import shutil
import subprocess
import sys
import re
from enum import Enum
from textwrap import dedent

from git import Repo, InvalidGitRepositoryError, NoSuchPathError

from prompt_toolkit import prompt, choice
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.shortcuts import confirm


class SubdirStatus(Enum):
    MERGED_CLEAN = 'merged_clean'
    MERGED_DIRTY = 'merged_dirty'
    UNMERGED = 'unmerged'
    NOT_A_WORKTREE = 'not_a_worktree'


def _run_git_cmd(path, cmd, *args):
    result = subprocess.run(['git', cmd] + list(args),
                   check=True,
                   cwd=path,
                            capture_output=True)
    return result.stdout.decode('utf-8')


def _open_shell(working_dir):
    shell_command = os.environ.get('SHELL', '/bin/bash')

    # TODO: Format this with prompt-toolkit
    print(
        dedent(
            f"""
            *****************************************************************************
            * Opening a shell in {working_dir} 
            * so you can run git commands to determine whether to clean up this         *
            * worktree.                                                                 *
            * Press Ctrl+D to exit back to the cleanup tool.                            *
            *****************************************************************************
            """
        )
    )

    process = subprocess.Popen([shell_command], cwd=working_dir)
    process.wait()


def _iterate_options(worktree_dir: str, subdir: str, subdir_path: str, subdir_status: SubdirStatus, subdir_branch: str=None):
    next_step = None

    match subdir_status:
        case SubdirStatus.NOT_A_WORKTREE:
            delete_option = 'Delete subdirectory'
        case SubdirStatus.MERGED_CLEAN:
            delete_option = 'Remove worktree'
        case _:
            delete_option = 'Remove worktree anyway'

    while next_step != 'ignore':
        next_step = choice(
            message='What would you like to do?',
            options=[('ignore', 'Ignore this worktree and continue cleanup'),
                     ('explore', 'Open shell to explore changes'),
                     ('delete', delete_option)],
        )

        if next_step == 'delete':
            if subdir_status == SubdirStatus.NOT_A_WORKTREE:
                if confirm(f"Are you sure you want to delete directory {subdir_path}?"):
                    shutil.rmtree(subdir_path)
                    next_step = 'ignore'
            else:
                git_cmd_args = ['remove']

                if subdir_status == SubdirStatus.MERGED_CLEAN:
                    delete_msg =  f"{subdir} is clean; remove worktree?"
                else:
                    delete_msg = f"Are you sure you want to delete worktree {subdir}? It contains changes."
                    git_cmd_args.append('--force')

                git_cmd_args.append(subdir)

                if confirm(delete_msg):
                    print(_run_git_cmd(worktree_dir, 'worktree', *git_cmd_args))
                    print(_run_git_cmd(worktree_dir, 'branch', "-D", subdir_branch))
                    next_step = 'ignore'

        elif next_step == 'explore':
            _open_shell(subdir_path)


def _is_branch_merged(repo: Repo, default_branch: str, worktree_dir: str, branch: str, branch_commit: str) -> bool:
    contains_output = repo.git.branch('--contains', branch_commit)
    containing_branches = re.findall(r'([\w\-]+)', contains_output)

    # TODO: use remote not local branch
    if default_branch in containing_branches:
        print(f"{branch} at commit {branch_commit} has been merged to {default_branch}")
        merged = True
    else:
        # Commands here taken from answer to:
        # https://stackoverflow.com/questions/43489303/how-can-i-delete-all-git-branches-which-have-been-squash-and-merge-via-github
        merge_base = _run_git_cmd(worktree_dir, "merge-base", default_branch, branch).rstrip()
        rev_parse_hash = _run_git_cmd(worktree_dir, "rev-parse", f"{branch}^{{tree}}").rstrip()
        commit_tree_hash = _run_git_cmd(worktree_dir, "commit-tree", rev_parse_hash, "-p", merge_base, "-m",
                                        "_").rstrip()
        cherry_output = _run_git_cmd(worktree_dir, "cherry", default_branch, commit_tree_hash)

        if cherry_output.startswith('-'):
            print(f"{branch} at commit {branch_commit} has been squashed-and-rebased to {default_branch}")
            merged = True
        else:
            merged = False
    return merged


def main():

    subdirs = None

    if len(sys.argv) == 1:
        path_completer = PathCompleter(
            only_directories=True,
            expanduser=True
        )
        worktree_dir = prompt("Select a directory that uses git-worktree: ", default='~/', completer=path_completer)
    else:
        worktree_dir = sys.argv[1]
        if len(sys.argv) > 2:
            subdirs = [sys.argv[2]]

    print(f"Checking worktree {worktree_dir}")

    worktree_dir = os.path.expanduser(worktree_dir)

    try:
        repo = Repo(worktree_dir)
    except InvalidGitRepositoryError as e:
        print(f"Error accessing repository: {e}")
        sys.exit(1)

    repo.remote().fetch(prune=True)

    default_branch_pattern = re.compile(r'^\*|master|main|dev')

    remote_branches = sorted([b.name.removeprefix('origin/') for b in repo.remote().refs])
    default_branches = [b for b in remote_branches if default_branch_pattern.match(b)]

    default_branch = choice(message="Select default remote branch: ", default='develop', options=[(b, b) for b in default_branches])

    if subdirs is None:
        subdirs = sorted([
            d for d in os.listdir(worktree_dir)
            if os.path.isdir(os.path.join(worktree_dir, d)) and not d.startswith('.')
               and not re.match(r'^\*|master|main|dev', d)
        ])
        subdirs.sort()

        print(f"Found {len(subdirs)} subdirectories")

    for subdir in subdirs:
        print(f"\nChecking subdir {subdir}")

        subdir_path = os.path.join(worktree_dir, subdir)

        try:
            worktree_repo = Repo(subdir_path)
        except NoSuchPathError:  # Could happen if path passed on command line
            print(f"{subdir_path} is not a valid path")
            continue
        except InvalidGitRepositoryError:
            print(f"{subdir} is not a valid git worktree")
            _iterate_options(worktree_dir, subdir, subdir_path, SubdirStatus.NOT_A_WORKTREE)
            continue

        try:
            subdir_branch = worktree_repo.active_branch.name
        except ValueError:
            print(f"Unable to get current branch for {subdir}")
            _iterate_options(worktree_dir, subdir, subdir_path, SubdirStatus.NOT_A_WORKTREE)
            continue

        print(f"Subdir {subdir} has active branch {subdir_branch}")

        merged = _is_branch_merged(repo, default_branch, worktree_dir, subdir_branch, worktree_repo.commit().hexsha)

        if merged:
            modified_files = worktree_repo.index.diff(None)
            untracked_files = worktree_repo.untracked_files

            if modified_files or untracked_files:
                subdir_status = SubdirStatus.MERGED_DIRTY
                print(f"There are {len(modified_files)} changed files and  {len(untracked_files)} changes in {subdir}")
                print(_run_git_cmd(subdir_path, 'status'))
            else:
                subdir_status = SubdirStatus.MERGED_CLEAN

        else:
            print(f"{subdir_branch} at commit {worktree_repo.commit()} has not been merged to {default_branch}")
            subdir_status = SubdirStatus.UNMERGED

        _iterate_options(worktree_dir, subdir, subdir_path, subdir_status, subdir_branch)

    # Now we've looped over subdirs, check for remaining unmerged local branches
    local_branches = {h.name: h for h in repo.heads if not default_branch_pattern.match(h.name) }

    if len(sys.argv) > 2:
        local_branches = {k: v for k, v in local_branches.items() if k == sys.argv[2]}

    for branch_name, head in local_branches.items():
        merged = _is_branch_merged(repo, default_branch, worktree_dir, branch_name, head.commit.hexsha)
        if merged and confirm(f"Delete merged local branch {branch_name}?"):
            print(_run_git_cmd(worktree_dir, 'branch', "-D", branch_name))


if __name__ == "__main__":
    main()
