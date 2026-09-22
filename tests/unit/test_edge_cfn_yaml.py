"""A reader for the CloudFormation YAML in deploy/, written without a YAML library.

The edge tests have to read deploy/edge.yml as a structure, not as text: a
property that is right in one cache behavior and missing in the next is
invisible to a regular expression over the file. No YAML package may be
installed here, and CloudFormation's short-form tags (!Ref, !Sub, !GetAtt,
!If, ...) are not plain YAML anyway, so this module reads the subset the
template is written in and turns each tag into the long form CloudFormation
itself expands it to: `!Ref X` becomes {"Ref": "X"}, `!GetAtt A.B` becomes
{"Fn::GetAtt": ["A", "B"]}, and every other tag `!Name v` becomes
{"Fn::Name": v}.

The subset: block mappings and sequences, a mapping that starts on a
sequence item's line, flow sequences and mappings (`[a, b]`, `{}`), plain,
single-quoted and double-quoted scalars, literal and folded block scalars
(`|`, `>-`, ...), full-line and trailing comments. Anything outside it (an
anchor, an alias, a merge key, a tab in the indentation, a duplicate key) is
refused with the line number, never guessed at, so a template that drifts
out of the subset fails its tests loudly instead of being half-read.

Other edge tests load this file by path, so the reader is defined once.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

EDGE_TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "edge.yml"

# A key is a quoted string, or a run of characters that CloudFormation keys use,
# followed by a colon that ends the line or is followed by a space. "AWS:SourceArn:"
# and "Fn::If:" are keys; "s3:GetObject" and "https://..." are not, because no colon
# in them is followed by a space.
_KEY = re.compile(
    r"""^(?P<key>'(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[A-Za-z0-9_.:/@-]+?)\s*:(?:[ \t]+(?P<rest>.*)|$)"""
)
_INTEGER = re.compile(r"^[-+]?[0-9]+$")
_TAGGED_FUNCTIONS = {
    "And", "Base64", "Cidr", "Equals", "FindInMap", "GetAZs", "If", "ImportValue",
    "Join", "Length", "Not", "Or", "Select", "Split", "Sub", "ToJsonString", "Transform",
}


class CfnYamlError(ValueError):
    """The text is outside the subset this reader accepts."""


def apply_tag(tag: str, value: Any) -> Any:
    """A short-form intrinsic function, in the long form CloudFormation expands it to."""
    name = tag[1:]
    if name == "Ref":
        return {"Ref": value}
    if name == "Condition":
        return {"Condition": value}
    if name == "GetAtt":
        if isinstance(value, str):
            resource, _, attribute = value.partition(".")
            return {"Fn::GetAtt": [resource, attribute]}
        return {"Fn::GetAtt": value}
    if name in _TAGGED_FUNCTIONS:
        return {f"Fn::{name}": value}
    raise CfnYamlError(f"unknown tag {tag}")


def _plain(token: str) -> Any:
    """A plain scalar, typed the way the template's readers need it."""
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token in ("null", "~", ""):
        return None
    if _INTEGER.match(token):
        return int(token)
    return token


def _strip_comment(text: str) -> str:
    """The line without a trailing comment. A '#' inside quotes is kept.

    A quote opens a quoted scalar only where one can start (after a space or an
    indicator), so the apostrophe in a plain "owner's" is not mistaken for one.
    """
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 1
                else:
                    quote = None
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = None
        elif char in "'\"" and (index == 0 or text[index - 1] in " \t[{,:"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


class _FlowReader:
    """One inline value: a flow collection, a quoted or plain scalar, possibly tagged."""

    def __init__(self, text: str, line_number: int) -> None:
        self.text = text
        self.pos = 0
        self.line_number = line_number

    def fail(self, message: str) -> CfnYamlError:
        return CfnYamlError(f"line {self.line_number}: {message}: {self.text!r}")

    def skip_spaces(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def read_top(self) -> Any:
        """The whole rest of a line. Outside a flow collection a plain scalar may hold commas."""
        self.skip_spaces()
        value = self.read_value(in_flow=False)
        self.skip_spaces()
        if self.pos != len(self.text):
            raise self.fail("unexpected text after the value")
        return value

    def read_value(self, in_flow: bool) -> Any:
        self.skip_spaces()
        if self.pos >= len(self.text):
            return None
        char = self.text[self.pos]
        if char == "!":
            end = self.pos
            while end < len(self.text) and self.text[end] not in " \t,[]{}":
                end += 1
            tag = self.text[self.pos:end]
            self.pos = end
            return apply_tag(tag, self.read_value(in_flow))
        if char in "&*":
            raise self.fail("anchors and aliases are outside the subset")
        if char == "[":
            return self.read_sequence()
        if char == "{":
            return self.read_mapping()
        if char in "'\"":
            return self.read_quoted()
        stops = ",]}" if in_flow else ""
        end = self.pos
        while end < len(self.text) and self.text[end] not in stops:
            if in_flow and self.text[end] == ":" and end + 1 < len(self.text) and self.text[end + 1] == " ":
                break
            end += 1
        token = self.text[self.pos:end].strip()
        self.pos = end
        return _plain(token)

    def read_quoted(self) -> str:
        quote = self.text[self.pos]
        index = self.pos + 1
        while index < len(self.text):
            char = self.text[index]
            if quote == "'" and char == "'":
                if index + 1 < len(self.text) and self.text[index + 1] == "'":
                    index += 2
                    continue
                break
            if quote == '"' and char == "\\":
                index += 2
                continue
            if quote == '"' and char == '"':
                break
            index += 1
        else:
            raise self.fail("unterminated quoted scalar")
        body = self.text[self.pos + 1:index]
        self.pos = index + 1
        if quote == "'":
            return body.replace("''", "'")
        # JSON's escapes are a subset of YAML's double-quoted ones, which is all
        # the template uses.
        return json.loads(f'"{body}"')

    def read_sequence(self) -> List[Any]:
        self.pos += 1
        items: List[Any] = []
        while True:
            self.skip_spaces()
            if self.pos >= len(self.text):
                raise self.fail("unterminated flow sequence")
            if self.text[self.pos] == "]":
                self.pos += 1
                return items
            items.append(self.read_value(in_flow=True))
            self.skip_spaces()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(self.text) and self.text[self.pos] != "]":
                raise self.fail("expected ',' or ']'")

    def read_mapping(self) -> dict:
        self.pos += 1
        result: dict = {}
        while True:
            self.skip_spaces()
            if self.pos >= len(self.text):
                raise self.fail("unterminated flow mapping")
            if self.text[self.pos] == "}":
                self.pos += 1
                return result
            key = self.read_value(in_flow=True)
            self.skip_spaces()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                raise self.fail("expected ':' in a flow mapping")
            self.pos += 1
            result[key] = self.read_value(in_flow=True)
            self.skip_spaces()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1


class _BlockReader:
    """The indentation structure of a whole document."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    def fail(self, index: int, message: str) -> CfnYamlError:
        shown = self.lines[index] if index < len(self.lines) else ""
        return CfnYamlError(f"line {index + 1}: {message}: {shown!r}")

    @staticmethod
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def content(self, index: int) -> str:
        line = self.lines[index]
        return _strip_comment(line[self.indent(line):])

    def significant(self, index: int) -> int:
        """The next line that holds something other than a comment."""
        while index < len(self.lines):
            line = self.lines[index]
            if line.lstrip(" ").startswith("\t"):
                raise self.fail(index, "a tab in the indentation")
            if self.content(index):
                return index
            index += 1
        return index

    @staticmethod
    def is_item(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def document(self) -> Any:
        start = self.significant(0)
        if start >= len(self.lines):
            return None
        value, end = self.node(start)
        end = self.significant(end)
        if end < len(self.lines):
            raise self.fail(end, "text outside the document's structure")
        return value

    def node(self, index: int) -> Tuple[Any, int]:
        column = self.indent(self.lines[index])
        if self.is_item(self.content(index)):
            return self.sequence(index, column)
        return self.mapping(index, column)

    def mapping(self, index: int, column: int) -> Tuple[dict, int]:
        result: dict = {}
        while True:
            index = self.significant(index)
            if index >= len(self.lines) or self.indent(self.lines[index]) < column:
                return result, index
            if self.indent(self.lines[index]) > column:
                raise self.fail(index, "unexpected indentation")
            content = self.content(index)
            if self.is_item(content):
                return result, index
            match = _KEY.match(content)
            if not match:
                raise self.fail(index, "expected 'key: value'")
            raw_key = match.group("key")
            key = _FlowReader(raw_key, index + 1).read_top() if raw_key[0] in "'\"" else raw_key
            if key in result:
                raise self.fail(index, f"duplicate key {key!r}")
            if key == "<<":
                raise self.fail(index, "merge keys are outside the subset")
            value, index = self.value(match.group("rest") or "", index, column, in_mapping=True)
            result[key] = value

    def sequence(self, index: int, column: int) -> Tuple[List[Any], int]:
        items: List[Any] = []
        while True:
            index = self.significant(index)
            if index >= len(self.lines) or self.indent(self.lines[index]) != column:
                if index < len(self.lines) and self.indent(self.lines[index]) > column:
                    raise self.fail(index, "unexpected indentation")
                return items, index
            content = self.content(index)
            if not self.is_item(content):
                return items, index
            after = content[1:]
            rest = after.lstrip(" ")
            item_column = column + 1 + (len(after) - len(rest))
            if rest and rest[0] not in "!'\"[{&*|>" and _KEY.match(rest):
                # A mapping that starts on the item's own line: read it as though
                # the dash were a space, so its first key sits in the column its
                # other keys are indented to.
                line = self.lines[index]
                self.lines[index] = line[:column] + " " * (item_column - column) + line[item_column:]
                value, index = self.mapping(index, item_column)
            else:
                value, index = self.value(rest, index, column, in_mapping=False)
            items.append(value)

    def value(self, rest: str, index: int, column: int, in_mapping: bool) -> Tuple[Any, int]:
        """What follows 'key:' or '- ' on line `index`, whose parent sits at `column`."""
        rest = rest.strip()
        tag: Optional[str] = None
        if rest.startswith("!"):
            tag, _, rest = rest.partition(" ")
            rest = rest.strip()
        if rest and rest[0] in "|>":
            value, index = self.block_scalar(rest, index, column)
        elif rest:
            value, index = _FlowReader(rest, index + 1).read_top(), index + 1
        else:
            following = self.significant(index + 1)
            deeper = following < len(self.lines) and self.indent(self.lines[following]) > column
            # YAML lets a mapping's sequence value sit in the key's own column.
            same_column_list = (
                in_mapping
                and following < len(self.lines)
                and self.indent(self.lines[following]) == column
                and self.is_item(self.content(following))
            )
            if deeper or same_column_list:
                value, index = self.node(following)
            else:
                value, index = None, index + 1
        return (apply_tag(tag, value) if tag else value), index

    def block_scalar(self, header: str, index: int, column: int) -> Tuple[str, int]:
        style, chomping = header[0], header[1:].strip()
        if chomping not in ("", "-", "+"):
            raise self.fail(index, "only the '-' and '+' block scalar indicators are in the subset")
        body: List[str] = []
        cursor = index + 1
        text_column: Optional[int] = None
        while cursor < len(self.lines):
            line = self.lines[cursor]
            if line.strip() == "":
                body.append("")
                cursor += 1
                continue
            if self.indent(line) <= column:
                break
            if text_column is None:
                text_column = self.indent(line)
            if self.indent(line) < text_column:
                raise self.fail(cursor, "a block scalar line less indented than its first line")
            body.append(line[text_column:])
            cursor += 1
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(body)
        else:
            paragraphs: List[str] = []
            current: List[str] = []
            for line in body:
                if line == "":
                    paragraphs.append(" ".join(current))
                    current = []
                else:
                    current.append(line)
            paragraphs.append(" ".join(current))
            text = "\n".join(paragraphs)
        if chomping == "-":
            pass
        elif chomping == "+":
            text += "\n" * (trailing + 1)
        elif body:
            text += "\n"
        # A blank line that ended the scalar belongs to whatever comes next.
        return text, cursor


def load_cfn_yaml(text: str) -> Any:
    """The template as dicts, lists and scalars, with short-form tags in long form."""
    return _BlockReader(text).document()


def load_edge_template() -> dict:
    return load_cfn_yaml(EDGE_TEMPLATE.read_text(encoding="utf-8"))


# --- the reader's own behaviour ----------------------------------------------------


def test_short_form_tags_become_the_long_form_cloudformation_expands_them_to() -> None:
    document = load_cfn_yaml(
        "A: !Ref Bucket\n"
        "B: !GetAtt Distribution.DomainName\n"
        "C: !Sub '${AWS::StackName}-pages'\n"
        "D: !Equals [!Ref AccessLogs, 'true']\n"
        "E: !Ref AWS::NoValue\n"
    )
    assert document == {
        "A": {"Ref": "Bucket"},
        "B": {"Fn::GetAtt": ["Distribution", "DomainName"]},
        "C": {"Fn::Sub": "${AWS::StackName}-pages"},
        "D": {"Fn::Equals": [{"Ref": "AccessLogs"}, "true"]},
        "E": {"Ref": "AWS::NoValue"},
    }


def test_a_tag_can_carry_a_block_sequence_or_a_folded_scalar() -> None:
    document = load_cfn_yaml(
        "Logging: !If\n"
        "  - LogsOn\n"
        "  - Bucket: !GetAtt LogBucket.DomainName\n"
        "    Prefix: cloudfront/\n"
        "  - !Ref AWS::NoValue\n"
        "Policy: !Sub >-\n"
        "  default-src 'none';\n"
        "  connect-src https://${ApiDomainName}\n"
        "Next: 1\n"
    )
    assert document["Logging"] == {
        "Fn::If": [
            "LogsOn",
            {"Bucket": {"Fn::GetAtt": ["LogBucket", "DomainName"]}, "Prefix": "cloudfront/"},
            {"Ref": "AWS::NoValue"},
        ]
    }
    assert document["Policy"] == {"Fn::Sub": "default-src 'none'; connect-src https://${ApiDomainName}"}
    assert document["Next"] == 1


def test_keys_with_colons_are_keys_and_values_with_colons_are_values() -> None:
    document = load_cfn_yaml(
        "Condition:\n"
        "  StringEquals:\n"
        "    AWS:SourceArn: arn:aws:cloudfront::1:distribution/X  # trailing comment\n"
        "Action:\n"
        "  - s3:GetObject\n"
        "  - 'https://example.com/#fragment'\n"
        "Empty: {}\n"
        "Methods: [GET, HEAD]\n"
    )
    assert document == {
        "Condition": {"StringEquals": {"AWS:SourceArn": "arn:aws:cloudfront::1:distribution/X"}},
        "Action": ["s3:GetObject", "https://example.com/#fragment"],
        "Empty": {},
        "Methods": ["GET", "HEAD"],
    }


def test_scalars_are_typed_only_where_unquoted() -> None:
    document = load_cfn_yaml("A: true\nB: 'true'\nC: 300\nD: '300'\nE: it's plain\nF: ''\n")
    assert document == {"A": True, "B": "true", "C": 300, "D": "300", "E": "it's plain", "F": ""}


def test_block_scalars_keep_or_fold_their_lines() -> None:
    document = load_cfn_yaml("A: |\n  one\n  two\nB: >\n  one\n  two\n\n  three\nC: >-\n  x # not a comment\n")
    assert document == {"A": "one\ntwo\n", "B": "one two\nthree\n", "C": "x # not a comment"}


def test_a_list_may_sit_in_its_key_s_own_column() -> None:
    assert load_cfn_yaml("Items:\n- a\n- b: 1\n  c: 2\nAfter: x\n") == {
        "Items": ["a", {"b": 1, "c": 2}],
        "After": "x",
    }


@pytest.mark.parametrize(
    "text",
    [
        "A: 1\nA: 2\n",
        "A: &anchor 1\nB: *anchor\n",
        "A:\n\t- 1\n",
        "A: !Bogus x\n",
        "A: 'unterminated\n",
        "A: 1\n   B: 2\n",
    ],
)
def test_anything_outside_the_subset_is_refused_rather_than_guessed(text: str) -> None:
    with pytest.raises(CfnYamlError):
        load_cfn_yaml(text)


def test_the_edge_template_is_inside_the_subset() -> None:
    template = load_edge_template()
    assert set(template) >= {"AWSTemplateFormatVersion", "Parameters", "Resources", "Outputs"}
