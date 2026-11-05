"""Validate frozen input schemas without allowing network schema retrieval."""

from jsonschema import Draft202012Validator, SchemaError


def input_validator(schema):
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Agent input schema is not valid JSON Schema 2020-12") from exc
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for keyword in ("$ref", "$dynamicRef"):
                if keyword in node and not node[keyword].startswith("#"):
                    raise ValueError("Agent input schemas must use local fragment references")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return Draft202012Validator(schema)
