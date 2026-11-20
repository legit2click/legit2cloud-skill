# Install the legit2cloud skill (for Claude Code)

A Claude skill that lets Claude generate **any** media on the legit2cloud AI
platform — music, voice, sound effects, images, video, short clips from long-form
video, news articles, and podcasts — and chat with the hosted language models
(text and vision, OpenAI-compatible, with `l2c/auto` picking the cheapest model
that fits) — with one API key.

## 1. Get your API key

1. Sign up at **https://legit2compute.com/developers/signup**. Signing up with Discord,
   Google, GitHub or Twitch includes free trial credit; an email + password sign-up
   starts at zero.
2. Copy your API key from the dashboard (starts with `l2ck_`).
3. Top up any time from the same dashboard.

## 2. Install the skill

Drop this whole `legit2cloud/` folder into your Claude skills directory:

- **Project-scoped** (this repo only): `<your-project>/.claude/skills/legit2cloud/`
- **Personal** (all your projects): `~/.claude/skills/legit2cloud/`

That's it — Claude Code auto-discovers skills there. No install step, no
dependencies (the bundled CLI is pure Python 3.8+ standard library).

## 3. Give Claude your key

Either set it in your shell before starting Claude:

```bash
export L2C_API_KEY=l2ck_your_key_here
```

…or just tell Claude your key in chat and ask it to generate something:

> "Here's my legit2cloud key l2ck_… — make me a 30-second upbeat lo-fi track."

Claude will discover what your key can generate (`me`), submit the job, wait, and
download the file. Ask for music, a voiceover, an image, a video, clips from a
YouTube link, an article, or a podcast — one key covers all of it. Ask it to run a
prompt "on legit2cloud" and it uses the LLM gateway (`l2c/auto` by default).

## Keep your key secret

It can spend your credit. Don't paste it into public code, commit it, or share it.
