# AreaDay

[English](README.md) | [简体中文](README.zh-CN.md)

AreaDay is distributed as a self-contained Skill for Codex and WorkBuddy.
This is a paid Skill, please ensure you have got a license of activation code from the author before you install it. 

#### Note for WorkBuddy Users

This Skill has been tested end-to-end on **WorkBuddy** using
**DeepSeek-V4-Flash**. Other compatible models may also work.

WorkBuddy's sandbox restricts certain system-level operations required by the
complete workflow. For example, `schtasks` may be blocked, preventing scheduled
tasks from completing.

We have not yet found a sufficiently reliable and user-friendly solution that
preserves all sandbox restrictions. For affected steps, **temporarily disabling
the WorkBuddy sandbox is currently the most reliable workaround**.

Disabling the sandbox reduces WorkBuddy's security isolation and may allow the
agent to access files and system resources available to your account. Only
proceed when you trust the Skill and task, avoid running WorkBuddy as
administrator, and **re-enable the sandbox when finished**.

If you are uncomfortable disabling the sandbox temporarily, please consider
this limitation **before purchasing a license**. We cannot currently guarantee
the complete workflow while the sandbox remains enabled.

## Install

For user, a simple way to install it is to send either of these links to Codex or WorkBuddy and say `安装一下`:

- Latest release: https://github.com/vvJesse/ResearchRamp/releases/latest
- This repository: https://github.com/vvJesse/ResearchRamp

The agent must read [INSTALL.md](INSTALL.md), select the delivery package for
the current computer, and complete installation and verification. The user does
not need to choose among GitHub files, extract a ZIP, move folders, run terminal
commands, or install dependencies.

Supported release platforms:

- 64-bit Windows on Intel or AMD
- Apple silicon Mac (M1 or newer)

Intel Macs are not currently supported.

## Learning without a brief

After the 30-question calibration, **今日复习** can offer a small set of
priority domain words even when no research brief has been generated. Their
Chinese meanings, optional English meanings, and an original-paper context are
prepared during domain setup, before the calibration page opens. Choosing
“需要学习” adds a word to the local review schedule; choosing “已经会了” keeps it
out. A later brief may add a new paper context for the same word, but cannot
replace its established meaning.

## Safety & Privacy

- Papers, PDFs, extracted text, notes, profiles, and vocabulary data are stored
  on the user's computer. AreaDay does not upload the original paper text or
  local file paths to its servers. Text tokenization and embedding inference
  also run locally, with ONNX Runtime telemetry explicitly disabled.
- To find papers, AreaDay sends search requests and related metadata to the
  enabled providers (OpenAlex and, when selected, arXiv). Those providers'
  terms and privacy policies apply to those requests.
- The licensed vocabulary service receives only the isolated-word test results
  and statistical features needed to build the personal vocabulary model. It
  does not receive papers, PDFs, extracted text, source URLs, or local paths.
- License activation sends the activation key, device identifier, platform, and
  version to the AreaDay activation service so it can verify the license and
  device limit. The installed license is stored locally after activation.
- OpenAlex API keys, activation keys, and local license files are credentials.
  Keep them private and do not commit or paste them into public issues,
  prompts, or repositories. AreaDay does not need access to your other files.
- AreaDay is a research and vocabulary aid. Review generated results before
  relying on them in research or other professional work.
