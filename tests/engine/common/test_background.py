from engine.common.background import character_name_groups


def test_only_declared_cast_aliases_participate_in_matching():
    saved = [{"name": "Alex", "aliases": ["Alexandra"]}]
    markdown = {"format": "markdown", "content": """
Ren is speaking to another character.
| Name | Aliases | Costume |
| --- | --- | --- |
| Ren | R, レン | blue shirt |

| Item | Price |
| --- | --- |
| clip | 5 |
"""}
    assert character_name_groups(saved, markdown) == [{"Alex", "Alexandra"}, {"Ren", "R", "レン"}]
    assert character_name_groups(saved, {"format": "json", "content": "{"}) == [{"Alex", "Alexandra"}]
    assert character_name_groups([], {"format": "json", "content":
        '{"characters":[{"name":"Ren","aliases":["R"]}],"note":"Other"}'}) == [{"Ren", "R"}]
