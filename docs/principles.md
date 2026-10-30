# Principles

The ideas that should hold however the app is built, whatever it looks like, and whichever part is being worked on. Each one notes what drifting away from it tends to look like. "You" means the person whose copy of the app it is.

## 1. Your words are the source of truth

What you write is kept exactly as you wrote it. Everything else (profile lines, signals, similarity, recommendations) is derived from those words and from your play history, and can be traced back to them. If a summary and the original disagree, the original wins.

*Drift looks like:* reviews rewritten into tidier summaries with the original thrown away; verbatim text being "cleaned up"; profile lines no one can trace to a review.

## 2. Every claim shows where it came from, and every change leaves a trace

The profiles separate three kinds of knowledge:

- **Stated:** you said it directly.
- **Observed:** a pattern across two or more games, with the games named.
- **Guess:** a hunch worth testing.

Guesses exist to be tested. A new review either supports one, and it gets promoted, or contradicts it, and it goes. When a profile changes, whether you edited it or the scheduled refresh did, a dated note says what changed and why.

*Drift looks like:* confident profile lines with no source; guesses that sit unresolved forever; inferences presented as things you said; a profile that shifts without anyone being able to say when or why.

## 3. Taste lives in how a game plays, not in its tags

Genres and Steam tags are a starting point. What actually decides whether a game lands is closer to questions like these: Is there a real decision every turn? Do the starting options play differently? Can you walk away mid-game? Will it survive a few weeks off? Does progress keep happening when you're not playing? Is the depth in strategy, or in rules you have to memorize?

Combolands and 9 Kings share nearly every tag, yet for the player who started this project they landed on opposite sides. That gap is what the project exists to capture, and it's what "similar" should mean.

*Drift looks like:* recommendations driven by tag overlap; similarity that calls two games alike because both say "Roguelite" and "City Builder"; reviews that capture genre but not feel.

## 4. Every recommendation explains itself

A pick names the parts of the profile it matches and the most likely reason it won't work. "Why this?" and "Why might this not work for me?" both get an answer. This applies to everything the recommender proposes, and when the review judge rejects something, it says why.

*Drift looks like:* a ranked list with scores and no reasons; a match percentage with nothing behind it; watch-outs left off because they make a pick look weaker; rejections with no reason, leaving the next round of research guessing.

## 5. Count the cost of trying a game

Time is usually the scarce resource. A recommendation weighs what a game asks for (price, learning curve, session length, whether it can be picked back up after a break) alongside how likely it is to be liked. In co-op, that cost lands on everyone who has to buy and learn it. A long, dense game can still be a great pick, but it's labeled as one to save for a stretch of free time, not slipped in as a casual suggestion. Games already owned come before games that cost money.

*Drift looks like:* a 200-hour sandbox with a steep learning curve recommended as a light pick; suggestions to buy something similar to a game already sitting unplayed in the library.

## 6. Facts about games carry a date

Review scores, prices, release dates, player counts and Early Access status change quickly. Anything time-sensitive is shown with the date it was checked, and it gets looked up again rather than assumed.

*Drift looks like:* a "Very Positive" from six months ago shown as current; a game still described as Early Access a year after its full release.

## 7. Writing a review should feel like talking about the game

A two-sentence review is a good review. Nothing beyond the game itself is required. Both parts of a co-op review can be a sentence or two, and noting who played should be quick. Structured details are offered, never demanded, and turning loose text into signals is the app's work. Follow-up questions are few, and asked only when the answer would actually change a profile or the recommendations.

*Drift looks like:* required fields; long forms; questions asked because a field is empty rather than because the answer matters.

## 8. Who's playing changes the answer

Each lane has its own profile and inventory, and each named group has its own profile, because a good pick depends on who's at the table. In a co-op review, your own take and your read of how the group found it stay separate. What you loved isn't automatically what they loved, and co-op recommendations should rest on how those people actually took to games. Every co-op review records who played. A named group's profile learns only from reviews tagged with that group, and tagging a group means everyone in it played. Subgroups aren't tracked as their own thing: people tags cover them, and recommendations for a particular mix of people are worked out on demand rather than kept as a standing list.

*Drift looks like:* one merged list or one shared profile; your personal verdict standing in for the group's experience; the two parts of a co-op review blended into one; a co-op review with no record of who played; a group's profile shifting because of a session only some of them played; subgroups turning into tracked groups with profiles and inventories of their own.

## 9. Play history is evidence, not a verdict

Hours and recency say a lot: a thousand hours in RimWorld means something. But idle games rack up hours running in the background, co-op hours reflect who else was playing, and a game that was never launched might be backlog or might be avoided. Play history is for forming hunches and choosing what to ask about. Verdicts come from reviews.

*Drift looks like:* hours treated as a rating; a background idle game ranked as an all-time favorite; conclusions drawn from playtime without checking with you.

## 10. Every recommendation gets an outcome

A recommendation is a prediction. It stays in the inventory as it moves from recommended to in progress and played, or to declined, so the record of what was suggested and how it went is never lost. When you play it, the review (hit or miss, and why) goes back into the profiles and into how future picks are made. When you decline it, you give a reason, and that reason is evidence too: it shapes the profile and what research looks for next. A decline isn't permanent. If the game gets a version update, it's re-reviewed, and the reason it was declined is part of that review.

*Drift looks like:* declines with no reason attached; played or declined games deleted from the inventory; the same kind of game suggested again after being declined for a reason that still applies; a declined game coming back with no version update; no way to tell whether recommendations are getting better.

## 11. The inventory is small, fixed and refilled on request

Each lane aims for 20 games in the recommended state. Games in other states (in progress, played, declined) stay in the inventory but don't count toward the 20. Research runs only when you start it, covers all four lanes, and re-reviews earlier candidates that have had a version update before searching the web for new ones. It brings any lane that's short back up to 20. For a lane that already has 20 or more, it says so and asks first, and if you agree, it adds exactly 5. Nothing goes in without passing the review judge, and everything arrives with its reasons. The 20 and the 5 are fixed on purpose, for simplicity and consistency.

A run does up to three rounds on its own. Past that, it pauses, and each further round happens only when you choose to continue, one round per continue. You can cancel at any point. How much to invest in a hard-to-fill lane is your call, not the app's.

*Drift looks like:* research running on its own schedule; in-progress or played games counted toward the 20; a lane growing past 20 recommended games without you agreeing to it; a setting for the inventory size or the top-up amount; a fresh web search every run while updated earlier candidates sit unchecked; games added because they showed up in a search, without passing the judge; research looping past three rounds without asking; a single continue that runs several rounds.

## 12. The recommender proposes; the judge decides

Research has two distinct roles. The recommender is the authority on what gets proposed: it directs the web research, draws on the candidate history, and picks the list for each lane by how well games match the profile in the dimensions that matter. The review judge is the final check: it tests each proposal against the data and can reject single games or the whole list, which sends the recommender back for another round. Keeping the roles apart is the point. The recommender doesn't approve its own picks, and the judge doesn't go looking for replacements; it decides.

*Drift looks like:* the recommender writing straight into the inventory; the judge swapping in games of its own; a judge that approves whatever it's shown; the two roles collapsing into a single step.

## 13. The recommender is measured, and the measure is tunable

The recommender's score combines two signals: how often the judge accepts its proposals, and how its picks fare once you play or decline them. Each signal carries a weight, and the weights are adjustable. That contrast is deliberate: the inventory size and top-up amount are fixed for consistency, while the score's weights exist to be tuned as you learn what a good recommender looks like.

*Drift looks like:* a score built on only one of the two signals; weights buried where they can't be adjusted; a score that's calculated but never shown, so no one can tell whether the recommender is improving.
