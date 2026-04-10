# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file browser game: `tictactoe.html` contains all HTML, CSS, and JavaScript inline. No build step, no dependencies, no package manager.

To play: open `tictactoe.html` directly in a browser.

## Architecture

Everything lives in `tictactoe.html`:
- **CSS** — dark-themed grid layout; `.cell`, `.x`, `.o`, `.win` classes drive visual state
- **JS** — plain vanilla JS; `board` (9-element array), `currentPlayer`, and `gameOver` hold all game state; `WIN_LINES` defines the 8 winning combinations; `init()` resets the board; `handleClick()` drives turn logic; `checkWinner()` scans `WIN_LINES` after each move

## Git workflow

- `gh` CLI is installed at `~/.local/bin/gh`, authenticated as `bruce152`
- Remote: `https://github.com/bruce152/tic-tac-toe` (`origin/main`)
- Commit every change with a clean message and push to `origin main`
