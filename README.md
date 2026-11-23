# legit2cloud skill

An Agent Skill (`SKILL.md`) that lets Claude Code and other skill-aware agents generate media on
[legit2cloud](https://legit2compute.com) with one API key: music, cloned voice with saved tones,
sound effects, images, video, multi-shot vertical Shorts, lip-sync and motion video, short captioned
clips from long videos, articles and podcasts, plus an OpenAI-compatible chat endpoint (text and vision).

New in 2026.11.20: **the Smirky Turkey Challenge.** `cartoon` finds the funniest moment one person says
in a family video and has a cartoon turkey re-perform it with their real voice.

```bash
python legit2cloud/scripts/l2c.py cartoon --video dinner.mp4 -o turkey.mp4
```

## Install

**Claude Code plugin** (updates with `/plugin marketplace update`):

```
/plugin marketplace add legit2click/legit2cloud-skill
/plugin install legit2cloud@legit2cloud
```

**One line** (installs into Claude Code, plus Hermes Agent and OpenClaw if you have them; run again to update):

```bash
curl -fsSL https://legit2compute.com/install-skill.sh | sh
```

Windows PowerShell: `irm https://legit2compute.com/install-skill.ps1 | iex`

**By hand:**

```bash
git clone https://github.com/legit2click/legit2cloud-skill
cp -r legit2cloud-skill/legit2cloud ~/.claude/skills/
```

Then give your agent the key:

```bash
export L2C_API_KEY=l2ck_...   # from https://legit2compute.com/developers/signup
```

Then ask Claude for a song, a voiceover, an image, a video or a turkey. Details: [INSTALL.md](legit2cloud/INSTALL.md),
the skill itself: [SKILL.md](legit2cloud/SKILL.md), what changed: [CHANGELOG.md](legit2cloud/CHANGELOG.md).

- The bundled CLI is Python 3.8+ standard library only. `cartoon`, `lipsync` and `motion` also need `ffmpeg`.
- Jobs bill a prepaid credit balance on your key. Social sign-ups include trial credit.
- The newest version is always at https://legit2compute.com/developers/skill.zip, and `python legit2cloud/scripts/l2c.py version` tells you when yours is behind.
