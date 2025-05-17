# ~/.bashrc: executed by bash(1) for non-login shells.

# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac

# don't put duplicate lines or lines starting with space in the history
HISTCONTROL=ignoreboth

# append to the history file, don't overwrite it
shopt -s histappend

# for setting history length
HISTSIZE=1000
HISTFILESIZE=2000

# check the window size after each command
shopt -s checkwinsize

# make less more friendly for non-text input files
[ -x /usr/bin/lesspipe ] && eval "$(SHELL=/bin/sh lesspipe)"

# Function to get the current Git branch
parse_git_branch() {
    git branch 2> /dev/null | sed -e '/^[^*]/d' -e 's/* \(.*\)/(\1)/'
}

# Setting prompt colors
COLOR_RED='\[\033[31m\]'
COLOR_GREEN='\[\033[32m\]'
COLOR_YELLOW='\[\033[33m\]'
COLOR_BLUE='\[\033[34m\]'
COLOR_RESET='\[\033[00m\]'

# Custom Prompt Configuration
PS1="${COLOR_GREEN}\u@\h${COLOR_RESET}:${COLOR_BLUE}\w${COLOR_YELLOW}\$(parse_git_branch)${COLOR_RESET}\$ "

# Enable Color Support
if [ -x /usr/bin/dircolors ]; then
    test -r ~/.dircolors && eval "$(dircolors -b ~/.dircolors)" || eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
    alias fgrep='fgrep --color=auto'
    alias egrep='egrep --color=auto'
fi

# some more ls aliases
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'

# Alias definitions.
if [ -f ~/.bash_aliases ]; then
    . ~/.bash_aliases
fi

# enable programmable completion features
if ! shopt -oq posix; then
  if [ -f /usr/share/bash-completion/bash_completion ]; then
    . /usr/share/bash-completion/bash_completion
  elif [ -f /etc/bash_completion ]; then
    . /etc/bash_completion
  fi
fi

# ===== SKYVERN Configuration =====

# NVM configuration
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion

# Poetry configuration
export PATH="$HOME/.local/bin:$PATH"

# Poetry configuration
export PATH="$HOME/.local/bin:$PATH"
alias pshell="poetry shell"
alias prun="poetry run"
alias activate-venv="source \$(poetry env info --path)/bin/activate"

# Welcome message and useful commands
echo -e "\033[1;36m======================================\033[0m"
echo -e "\033[1;36m  Skyvern Development Environment     \033[0m"
echo -e "\033[1;36m======================================\033[0m"
echo -e "\033[0;33mRecommended first steps:\033[0m"
echo -e "  \033[0;32mbash ./docker/dev/min_setup.sh\033[0m - Run minimal setup script (recommended first try)"
echo -e "\033[0;33mUseful commands:\033[0m"
echo -e "  \033[0;32mpshell\033[0m - Activate Poetry environment"
echo -e "  \033[0;32mprun\033[0m - Run commands in Poetry environment"
echo -e "  \033[0;32mactivate-venv\033[0m - Activate Python virtual environment"
echo -e "\033[1;36m======================================\033[0m"