# House rules

Decisions producers have already made. The unattended editor reads this file before every edit and follows it instead of deciding again or asking. Its rules beat the editor's own judgment. Anything no rule covers gets flagged for review.

Producers edit the live rules in the Portal (Automatic editing → Editor settings → House rules), and "Remember this" fixes are added there. This file is the editor's default until a producer saves their own.

1. **Fresh graphics, every show.** Replace every burned-in graphic with a fresh 2026-package render: TV cards, name bars and dual thirds, the Follow card, and any logo or graphic fired by mistake. Cut the switcher's intro, logo card and end card, and use `InFocus Intro 2026.mp4` and `InFocus Outro 2026.mp4`.
2. **Every segment peaks at −6 dBFS.** That includes anchors, throws, packages, intro and outro. Anchor dialogue is levelled to about −16 LUFS first, every anchor segment within 3 dB of the others.
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
13. **Several recordings:** the best take of a segment can be in any recording. The last complete, clean read wins wherever it is. A take never spans two recordings.
14. **Camera feeds, the open, throws and close:** these are on M (the two-shot).
15. **Camera feeds, announcements:** each one is on the side set (L or R) of the anchor reading it, with that announcement's card on the TV. Decide who is reading from mouth movement, then the loudest feed, then the script order. When the recording also has a switched program file, its cuts are the starting point.
16. **Camera feeds, cutting:** cut on sentence boundaries, never in the middle of a word, and hold every shot for at least about 2 seconds.
17. **Camera feeds, graphics:** camera feeds have no burned-in graphics. Put name bars or the dual third on at each anchor's first M shot, and the Follow card once in the close.
