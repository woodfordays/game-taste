# Game Taste: Vision

*Last updated 2026-10-30. This describes what the project is for and why. It deliberately says nothing about how it's built. "You" means the person whose copy of the app it is.*

## In one sentence

Game Taste turns your reviews of the games you play into a living picture of what works for you and for the people you play with, and uses that picture to recommend what to play next.

## Who it's for

Anyone who wants game recommendations built on their own taste rather than on genre labels and bestseller lists. It's shared on GitHub as a container you run yourself, so each person has their own copy, with their own Steam library, reviews and profiles. It started as one player's tool and is shared so friends can use it for themselves.

## Why it exists

Steam, review sites and "best of" lists can tell you what a game *is*. They can't tell you why a particular game worked for you, and that's what decides whether the next one will.

The example that started this project is Combolands and 9 Kings. On paper they're near-twins: short-run roguelite builders where you place pieces on a grid, both well reviewed, both from the same corner of Steam. Combolands got played to the end. 9 Kings felt shallow inside a demo. The kings didn't play differently from each other, and most turns came down to a yes-or-no ("can I play this card?") rather than a real choice. No tag list separates those two games. The player's own words do.

The Steam library behind this project told the same story. Islanders and Combolands are often mentioned together; one got an hour, the other twenty. RimWorld, Factorio and Stellaris had well over two thousand hours between them, yet long games like these kept getting dropped after a busy stretch, because coming back meant relearning them.

Co-op adds a second question. How a game felt to you and how it went for the people you played it with aren't the same thing. A game you love can fall flat with the group, and one you're lukewarm on can be the one everyone wants to go back to. Recommendations for a co-op night should rest on how those particular people took to past games, not only on your own taste.

For a lot of people, time is the scarce resource. A bad pick costs money and an evening, and a game that doesn't survive a break often never gets picked up again. So the project's job is to capture *why* each game landed or didn't, for you and for the people you play with, and to recommend on that.

## The loop

Everything in the app serves one loop:

1. **Play** something.
2. **Write about it** in your own words, as loosely as telling a friend. Two sentences is a real review. For a co-op game, also say how it went for everyone else, and note who played.
3. **The profiles update.** The lane's profile, and the profile of any named group the review is tagged with, take in the new evidence. Hunches get confirmed or dropped, and the questions that would most sharpen the picture get asked.
4. **Recommendations re-rank** against the updated profiles, each with its reasons and its risks.
5. **Play one, or decline it with a reason.** How it went, or why it was turned down, is the next piece of evidence.

A recommendation is a prediction, and what happens next is how it gets checked. Misses and declines are as useful as hits, often more. Anything that doesn't feed this loop is secondary.

A second loop keeps the recommendations stocked. It runs only when you ask for it (see "Keeping the inventory stocked" below).

## Lanes, people and groups

### Four lanes, by head count

Lanes follow how many people are playing, you included.

- **Solo.** Just you, on your own schedule.
- **Two players.** You and one other person. The question is what works for both of you, not what you like that happens to support two players.
- **Three to four players.** A small group, such as a regular trio or a group of four.
- **Five or more players.** The bigger nights, when most or all of your regulars are on.

In every co-op lane, supporting the head count is the first filter, not an afterthought.

Each lane has its own profile and its own inventory of recommendations. A game can sit in more than one lane, but it has to earn its place in each one separately. A great solo game doesn't become a co-op pick just because it has a multiplayer mode.

### People and named groups

If you play with a consistent group, chances are they don't all play everything. Subgroups form around particular games that the others don't play, and the other way around. So the app keeps track of who was actually there.

- **Named groups** are the combinations you set up because they play together regularly. A review can be tagged with a named group, which means everyone in it played. Each named group has its own profile, and only reviews tagged with that group update it.
- **People are tagged on reviews.** Every co-op review records who played, and people tags are how subgroups are handled. The app doesn't track subgroups as such. When you want ideas for a particular mix of people, such as the four who are online tonight, you ask on demand, and the answer draws on the reviews tagged with those people. These on-demand recommendations are separate from the lane inventories.

### Co-op reviews have two parts

For a co-op game, whatever the head count, you write:

- **Your own take:** what you thought of the game.
- **The group's experience, as you saw it:** how it went for the people you played with.

Both parts are your words. The second is your read of the room, not a review written by the others. It's what keeps the co-op profiles from being your own taste in disguise.

## What the app does

Described by what you experience, not by how it works underneath.

- **Write reviews.** Quick to start, with nothing required beyond the game and a few words. Your text is kept exactly as written. Structured details (verdict, hours, whether it felt walk-away-able, whether it was too light or too heavy) help but are optional. For a co-op game, the review has the two parts above, a list of who played, and the named group if it was one. Pulling signals out of the text is the app's job, not yours.
- **Backfill your history.** Reviews of games you played long ago are as welcome as new ones, so profiles can start from real history rather than a blank page.
- **Read the profiles.** One for each lane and one for each named group, in plain language you can read and argue with. Every line shows where it came from: something you said, a pattern across several games, or a hunch waiting to be tested. They're documents, not scores.
- **Work through each lane's inventory.** Each lane keeps its recommendations along with where each one stands: recommended, in progress, played or declined. The goal is always 20 in the recommended state. Every pick says which parts of the profile it matches, what the main risk is, and when its facts (price, review score, release status) were last checked. Games you already own but haven't played come before anything you'd have to buy. Any pick can be declined, with a reason.
- **Ask about a specific set of people.** Because reviews record who played, you can ask on demand what would suit exactly the people who are around tonight. These answers are worked out when you ask, not kept as a standing list.
- **Run research when you want new candidates.** See "Keeping the inventory stocked" below.
- **Browse the library and the inventory.** Owned games, played games and recommended games, with artwork and descriptions, so it feels like a personal shelf rather than a spreadsheet.
- **Let the profiles keep themselves current.** On a schedule, the app reads new reviews, updates the lane and group profiles, refreshes game details, re-ranks each inventory, and notes what changed. You read the result; you don't have to operate the process.
- **Make its own recommendations.** A homegrown recommender decides what gets proposed. It judges games by how alike they are in the ways that matter to you (how decisions feel, how long a session runs, how progression works, whether it survives a break) rather than by genre labels, and when it says two games are similar, it can say why. A separate review judge checks its proposals before anything reaches the inventory.

## Keeping the inventory stocked

Each lane keeps an inventory of recommended games. Games don't leave it; they move through states, so the inventory doubles as a record of what was suggested and how it turned out.

- **Recommended:** the starting state for every game added.
- **In progress:** you've started playing it.
- **Played:** you're done with it, and a review captures how it went.
- **Declined:** you turned it down, with a reason.

The goal is 20 games per lane in the recommended state. Starting a game or declining one is what opens a slot.

### Research

Research runs only when you start it, and one run covers all four lanes. Two roles do the work:

- **The recommender** is in charge of what gets proposed. It starts with earlier candidates rather than the open web. Every game that has ever been a candidate is remembered, including ones you declined and ones the judge rejected, and any that has had a version update since it was last considered (a 1.0 release, a major patch) gets another look. They were candidates for a reason, and checking them first saves searching from scratch. Then the recommender directs web research to find new candidates, and from everything gathered it picks the list to propose for each lane.
- **The review judge** has the final say. It checks each proposed game against the data (the lane's profile, the reviews behind it, the game's current facts) to make sure it's a good recommendation. For a game you declined before, that includes whether your reason still applies. The judge can reject individual games or the whole proposed list.

One pass of proposing and judging is a round. A rejection sends the recommender back to research for another round, knowing what was rejected and why. Up to three rounds run automatically. If the inventory still isn't replenished after that, the run pauses and you decide: continue for one more round, or stop there. Each continue buys exactly one round, so every extra round is a deliberate choice about how much more time and cost to put in. You can cancel a run at any point.

### When a lane is already full

If a lane already has 20 or more recommended games when research starts, the app says so and asks whether to keep going for that lane. If you say yes, it adds exactly 5 more to it. Lanes that are short get filled without asking. Both numbers are fixed on purpose, for simplicity and consistency. They aren't settings.

### Keeping score

The recommender has a score, so it's clear whether it's getting better or worse. The score combines two signals: how often the judge accepts what it proposes, and how its picks fare once they reach you (played and enjoyed, or declined). Each signal has a weight, and the weights are adjustable so the score can be tuned. Unlike the 20 and the 5, the weights are meant to change.

## What good looks like

- Writing a review takes about as long as telling someone about the game.
- You read a profile and recognize yourself, or your group, in it. Now and then it points out a pattern you hadn't noticed but agree with.
- On a game night, you can ask what would work for exactly the people who showed up, and get an answer grounded in how those people have taken to past games.
- You trust the recommendations enough to act on them, and when one misses or gets declined, the profile learns something specific from it.
- Every lane always has something worth playing, without the list sprawling past the point where anyone reads it.
- Fewer games bounced off after an hour; more played to the end.
- The owned backlog gets played before money gets spent.
- The recommender's score climbs over time, and its picks are ones a search for "best games of the year" wouldn't have turned up on its own.
- A friend can get their own copy running and start getting useful recommendations without help.

## What it isn't

- **Not a hosted service, for now.** Each person runs their own copy for themselves, and copies are fully separate: no shared accounts, no central server, and nothing synced between friends' copies, even when they play together. Running it as a connected service that links everyone is a possible future if the project takes off, but it's out of scope today, so nothing is built toward it yet.
- **Not a game database.** Game details are there to support decisions and make browsing pleasant, not to catalog everything on Steam.
- **Not a social or public review site.** Reviews are notes written for your profiles, not posts written for an audience.
- **Not a survey of the group.** Group reviews are your own read of how a session went. The people you play with don't have to do anything.
- **Not a playtime scoreboard.** Hours played are evidence about taste, not a goal or a rating.
- **Not a black box.** A recommendation that can't explain itself isn't finished.
- **Not demanding.** Half-thoughts, missing fields and "it was fine, I guess" are all valid input.

## Where it started

The loop was worked out by hand first, in conversation, with plain files: one review per game, a taste profile with tagged evidence and a changelog, ranked lists for each way of playing, and a pull of the Steam library with hours played. That prototype showed the loop works. The app carries the same ideas forward, lets the loop run without someone at the keyboard driving it, and packages it so other people can run it for themselves.

## Still open

Nothing is undecided at the moment. When a new question comes up, it goes here and gets a deliberate answer, rather than being settled quietly by whatever falls out of the build.

See also [principles.md](principles.md) for the ideas that should hold however the app is built, and [concepts.md](concepts.md) for the project's vocabulary.
