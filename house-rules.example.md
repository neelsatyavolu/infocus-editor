# House rules

Decisions producers have already made. The unattended editor reads this file before every edit and follows it instead of deciding again or asking. Its rules beat the editor's own judgment. Anything no rule covers gets flagged for review.

The live copy lives in the editor's data folder (`~/InFocusEditor/house-rules.md`), not in this repo, because remembered fixes can mention real shows and people. This file seeds it.

1. **Fresh graphics, every show.** Replace every burned-in graphic with a fresh 2026-package render: TV cards, name bars and dual thirds, the Follow card, and any logo or graphic fired by mistake. Cut the switcher's intro, logo card and end card, and use `InFocus Intro 2026.mp4` and `InFocus Outro 2026.mp4`.
2. **Every segment peaks at −6 dBFS.** That includes anchors, throws, packages, intro and outro. Anchor dialogue is levelled to about −19.5 LUFS first.
3. **Names use the spelling the Portal gives** (`brief.anchors`), even when the filmed graphic used a nickname or a short form.
4. **Fix obvious typos on filmed cards** (a missing letter, a doubled word). Keep every other word exactly as filmed.
5. **Takes:** keep the last complete, clean read of each segment. Keep live announcement runs as they aired, including the switcher's cuts. When two reads are equally good, use the later one.
6. **Full-screen flash with speech under it:** cut the flash and cover what's left with a held two-shot frame (`fixups.py hold`). Never drop words.
7. **A logo or graphic fired early over a clean set wall:** cover it with a clean plate from a nearby frame (`fixups.py patch`).
8. **An old graphic's last frames outlast the fresh one:** put a small clean plate under the fresh graphic (`fixups.py under`).
9. **A dual third or name bar runs as long as the old one did**, so its out-animation covers the old one's.
10. **Packages come only from the publishing queue** (`brief.packages`, in order). If the anchors throw to a package that isn't there, use a placeholder slate and flag it.
11. **Explicit lyrics in package music go to review**, never to auto-publish.
12. **Package design rules:** name plates are square in Soft White `#ECEFEA`. TVs idle on `09 TV Standby`. The Follow card sits top left.
