# legit2cloud skill — changelog

Newest first. The version you have is in `VERSION` (and at the top of SKILL.md). The newest
one is in `GET https://api.legit2compute.com/skill` (no key needed), in `me` → `skill`, and in
the `X-L2C-Skill-Latest` header of every API response. Update:
`curl -fsSL -o /tmp/legit2cloud-skill.zip https://legit2compute.com/developers/skill.zip`
and unzip it over the old folder.

## 2026.11.22
- The same voice in another language: add `"language": "es"` (or `"hi"`) to a voice job and write
  the text in that language. Spanish and Hindi are tested; 17 more languages are accepted. One
  language per job, 900 characters per line, tones and emotion settings do not apply there. It does
  not translate: use `chat` for that first. A language we cannot speak, or a line that is too long,
  is refused at once with a 400 that says what to change.

## 2026.11.20
- Holiday cartoons: `cartoon --video dinner.mp4` finds the funniest moment one person carries
  and has a cartoon turkey (elf, pilgrim, snowman, gingerbread, or your own) re-perform it with
  the real audio. The Smirky Turkey Challenge. `--plan-only` shows the line before you pay
  for the render.

## 2026.11.19
- Saved voices and tones: save a character's voice once (`voices save`), add tones
  (`voices tone maple happy --emotion happy=0.8`), then send `"voice": "maple:happy"` on every
  voice job. Same voice, fixed seed and pronunciation fixes on every line. `voices audition`
  renders one line in every tone so you can compare them.
- The voice example now sends `emotion` as an object (`{"calm": 0.6}`). A plain word such as
  `"calm"` was ignored.
- Skill versions: this changelog, a `VERSION` file and `python scripts/l2c.py version`. The CLI
  tells you when a newer skill is out.
- Lip-sync: one job renders up to 5 minutes; the command waits up to 12 hours.

## Before 2026.11.19
Unversioned. If your SKILL.md has no "Skill version" line, it is older than everything above:
update it.
