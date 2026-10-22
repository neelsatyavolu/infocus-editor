# Past announcement cards

Real cards from aired shows, for writing a card when none was filmed (SKILL.md step 4). `brief.pastCards` holds newer ones from the Portal; prefer those when there are several.

## The style

- **Title:** the club or event, 2–5 words. A club's own name is fine ("PALY Math Club").
- **Points:** 2–4 fragments of 2–7 words. When and where come first, then the one thing to know or do (a price, a link, "Wear orange", "Bring your student ID").
- **No sentences.** No "Come swing by", no background, no who-is-organizing unless the audience needs the name to act.
- Dates as said on air ("October 28th & 29th", "Monday Oct 12"), times short ("6:30-8:30 PM", "9:05AM").

## From the script to the card

Script (2026-10-21):
> Breaking Down the Walls is coming up on October 28th and 29th. Student captains will participate in training on October 27th and a full day of service. Student crew members can sign up to help on either October 28th or 29th. Both captains and crew members will receive a free T-shirt, lunch, and community service hours.

Filmed card:
```json
{ "title": "Breaking Down Walls", "points": ["October 28th & 29th", "Free T-shirts, Lunch, and Service Hours"] }
```

Too long (an editor's first try; don't do this):
```json
{ "title": "Breaking Down the Walls", "points": ["October 28 and 29", "Student captains: training October 27", "Crew: sign up for October 28 or 29", "Free T-shirt, lunch and community service hours"] }
```

## More filmed cards

```json
{ "title": "College visits tomorrow", "points": ["Michigan State 9:05AM", "Northeastern University, 9:50 AM", "University of Arizona 10:45 AM"] }
{ "title": "College Fair", "points": ["Monday Oct 12", "6:30-8:30 PM", "Bring your student ID to sign up", "Peery center"] }
{ "title": "DIY Magazines", "points": ["Mini Magazines", "zine-making workshop", "October 4 and 18"] }
{ "title": "PALY Dance Pies", "points": ["Frozen Pies from Gizdich Ranch", "25 dollars each"] }
{ "title": "PALY Math Club", "points": ["October 21st After School", "Hosting a Mock Math AMC", "In Room 855", "tinyurl.com/palymathdiscord"] }
{ "title": "Unity Day", "points": ["Wednesday Oct 21st", "Wear Orange to School"] }
{ "title": "France & Spain Trip Info Meeting", "points": ["October 27th in Room 808", "6:30 to 7:30"] }
```

(The France & Spain card also had the hosting teacher's name as its first point.)
