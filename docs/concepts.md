# Concepts

The vocabulary of the project. These are ideas, not a data model. How they're stored is up to the build, but what they mean shouldn't drift. "You" means the person whose copy of the app it is.

## Games and people

**Game.** A specific title on Steam (PC). A game has facts that change over time (description, artwork, developer, release date, price, review score, Early Access status, supported player count), and those facts are always "as of" a date.

**Library.** The games you own on Steam, with hours played and when each was last played. It includes games bought and never launched, which are the first place to look before buying anything new.

**Person.** Someone you play with. People are tagged on co-op reviews to record who was there. People tags are also how subgroups are handled; there's no separate subgroup concept.

**Named group.** A set of people you define because they play together regularly. A review tagged with a named group means everyone in it played. Each named group has its own profile, and only reviews tagged with that group update it.

## Reviews

**Review.** Your note on a game you've played or tried. At its heart is the raw feedback, in your words. Around it, optionally:

- *Status:* finished, playing, demo, or dropped.
- *Verdict:* loved, liked, mixed, or disliked.
- *What hooked* and *what bugged.*
- *Feel:* whether it was walk-away-able, session or run length, and complexity (too light, about right, too much).

A game gets one review for each lane it's played in. A game can be great with friends and dull alone, and each lane's profile learns from the review about that lane.

A review is a moment in time. A demo impression and a later opinion of the full release are both worth keeping, and a backfilled review of something you played years ago counts as much as a new one.

**Co-op review.** A review of a game played with others, at any head count. It has two parts, both written by you:

- *My take:* what you thought of the game.
- *Group experience:* how it went for the people you played with, as you saw it.

It also records who played, and the named group if it was one. The group experience is your perception of the session, not the other players' own opinions.

**Profile signal.** What a review says about taste in general, beyond the one game. "Each turn needs several real options" is a signal; "9 Kings felt repetitive" is the review it came from. Signals are the bridge from reviews to profiles.

## Profiles

**Profile.** A plain-language description of what works and what doesn't. There's one for each lane and one for each named group. Each covers what's loved, dealbreakers, patterns in play history, hunches still being tested, open questions, and a changelog.

**Evidence tags.** Every line in a profile is one of:

- **Stated:** you said it directly.
- **Observed:** a pattern across two or more games, which are named.
- **Guess:** a hunch, waiting for a review to confirm or contradict it.

**Hunch.** A guess with a test attached. Each new review is a chance to promote one or retire it.

**Open question.** Something a profile can't answer yet, where the answer would change recommendations, such as whether deckbuilding is a plus or whether Early Access is a turn-off. Follow-up questions come from here.

**Changelog.** A dated record of how a profile changed and why.

**Taste dimensions.** The qualities that actually decide whether a game lands. The reviews that started this project point to things like:

- whether every turn offers a real decision
- whether starting options (characters, factions, kings) play differently
- whether you can walk away mid-game without penalty
- session or run length
- whether the game survives a break or needs relearning
- depth of upgrades, skill trees and progression
- whether progress continues while you're not actively playing
- where the complexity lives: in strategy and builds, or in rules to memorize

The list grows as reviews reveal new ones, and co-op reviews will add their own. These are the terms the reviews, the profiles and the recommender should share.

## Recommending

**Lane.** One of four ways to play, set by head count (you included), each with its own profile and inventory:

- *Solo:* just you.
- *Two players:* you and one other person.
- *Three to four players:* a small group.
- *Five or more players:* the bigger nights.

A game only qualifies for a co-op lane if it supports that many players.

**Recommendation.** A game suggested for a lane. It always carries:

- why it fits, in terms of specific profile lines
- the main watch-outs or risks
- its facts, with the date they were checked
- where the candidate was found: web research, the candidate history, or the owned backlog

A recommendation stays in its lane's inventory for good, moving through the inventory statuses below.

**Decline.** Turning down a recommendation, with a reason. The reason is evidence: it feeds the lane's profile and shapes what future research looks for. A decline isn't permanent: if the game gets a version update, the next research run re-reviews it with the reason in mind.

**Inventory.** Every game recommended for one lane, with its status. The target is 20 games in the recommended state. Games in other states stay as a record but don't count toward the 20.

**Inventory status.** Where a recommendation stands:

- *Recommended:* the starting state for every game added.
- *In progress:* you've started playing it.
- *Played:* you're done with it, and a review captures how it went.
- *Declined:* you turned it down, with a reason.

This is separate from a review's status, which describes where you are with a game whether or not it was ever recommended.

**Owned backlog.** Games in the library that fit a profile but have barely been played. They're checked before anything new is recommended for purchase.

**Candidate history.** Every game that has ever been a candidate, whether it was added, rejected by the review judge, or later declined, along with the version it was considered at. It's the first place the recommender looks.

**Recommender.** The homegrown system that decides what gets proposed. It directs web research, draws on the candidate history, and picks the games to propose for each lane by how well they match the profile in the taste dimensions. It's the authority on what's proposed, not on what's accepted.

**Review judge.** The final check on the recommender's proposals. It tests each game against the data (the lane's profile, the reviews behind it, the game's current facts) and can reject single games or the whole proposed list. Every rejection comes with a reason and sends the recommender back for another round.

**Round.** One pass of the recommender proposing and the judge reviewing. A research run does up to three rounds on its own. After that it pauses, and each further round runs only when you choose to continue, one round per continue.

**Research run.** An on-demand pass that covers all four lanes. The recommender re-checks the candidate history for games with a version update, directs web research for new candidates, and proposes a list for each lane. The review judge accepts or rejects them, game by game or as a whole list, and rejections lead to further rounds until every lane has at least 20 recommended games, or until you stop. For a lane that already has 20 or more, the run says so and asks whether to continue; if you agree, it adds exactly 5 to that lane. Neither number is configurable. You can cancel a run at any point.

**Recommender score.** How well the recommender is doing, combining two weighted signals: how often the judge accepts its proposals, and how its picks fare once they reach you (played and enjoyed, or declined). The weights are adjustable, so the score can be tuned.

**Combination query.** Asking, on demand, what suits a specific set of people, using the reviews tagged with those people. This is how subgroups get recommendations. The answers are worked out fresh each time and don't go into a lane's inventory.

**Similarity.** How alike two games are in the taste dimensions that matter. Shared tags are weak evidence; shared feel is strong evidence. It's the main lens the recommender uses. A good similarity can be said out loud: "both give you several real options every turn, and both have runs of about half an hour."

**Refresh.** The scheduled pass that reads new reviews, updates lane and group profiles, refreshes game facts and artwork, and re-ranks the recommended games in each inventory. Every refresh leaves a changelog entry. Bringing in new games is the research run's job, not the refresh's.

## How it fits together

Reviews, tagged with who played, and play history from the library produce **signals**. Signals shape the **lane profiles** and the profiles of any **named groups** the review is tagged with. The profiles, expressed in **taste dimensions**, are what the **recommender** measures **similarity** by. In a **research run**, started by you, the recommender re-checks the **candidate history**, directs web research and proposes games, and the **review judge** decides which ones enter each lane's **inventory**, over as many **rounds** as it takes, or as many as you allow. Playing a recommendation produces the next **review**; declining one produces a **reason**. Both feed the **recommender score**, and the loop starts again.
