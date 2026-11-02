"""The README's research section: what each job and Tavily get, and what a run costs."""

from pathlib import Path

README = Path(__file__).parent.parent / "README.md"


def research_section():
    """The "Research and recommendations" section, with runs of white space as one."""
    text = README.read_text(encoding="utf-8")
    section = text.split("## Research and recommendations", 1)[1].split("\n## ", 1)[0]
    return " ".join(section.split())


def test_the_research_section_says_what_reaches_each_job():  # Ruling 33
    section = research_section()
    for said in (
        # The judge gets more of each review than its words.
        "the names of the people you played with, the group, the tags and the Steam hours",
        # A local judge isn't enough to keep everything at home.
        "Recommender still gets the profile lines, which are written from your reviews",
        "use a local server for the Recommender",
        "Tavily always gets the searches.",
    ):
        assert said in section


def test_the_research_section_gets_the_small_things_right():  # Ruling 33
    section = research_section()
    for said in (
        "A lane that already has 20 or more",
        "with a Steam sync if Steam is connected",
        "Two jobs share the work.",
        "your owned games with under 2 hours of play",
        "a full run multiplies them",
        "The cost depends on the provider you chose.",
    ):
        assert said in section
    for gone in (
        "Steam sync included",
        "Two models share the work",
        "little play time",
        "already has 20 gets",
    ):
        assert gone not in section
