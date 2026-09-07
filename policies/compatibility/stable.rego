package comparison_after_resolution

import rego.v1

# Weaver supplies the resolved candidate as input, resolved baseline as data.
contract_current_attributes := {a.key: a | some a in input.registry.attributes}
contract_current_metrics := {m.name: m | some m in input.registry.metrics}

contract_enum(kind) if { is_array(kind.members) }
contract_type_changed(old, current) if { not contract_enum(old); old != current }
contract_type_changed(old, current) if { contract_enum(old); not contract_enum(current) }
contract_type_changed(old, current) if {
    contract_enum(old)
    contract_enum(current)
    # Enum additions and documentation edits preserve compatibility.
    previous := {[m.id, m.value] | some m in old.members}
    candidate := {[m.id, m.value] | some m in current.members}
    count(previous - candidate) > 0
}

contract_compatibility_finding(id, name, message) := {
    "id": id, "level": "violation", "message": message,
    "context": {"name": name},
}

deny contains contract_compatibility_finding("compatibility.attribute_removed", old.key,
    "A stable baseline attribute was removed or renamed.") if {
    some old in data.registry.attributes
    old.stability == "stable"
    not contract_current_attributes[old.key]
}

deny contains contract_compatibility_finding("compatibility.attribute_type", old.key,
    "A stable baseline attribute's type definition changed.") if {
    some old in data.registry.attributes
    old.stability == "stable"
    current := contract_current_attributes[old.key]
    contract_type_changed(old.type, current.type)
}

deny contains contract_compatibility_finding("compatibility.attribute_stability", old.key,
    "A stable attribute regressed to an unstable maturity level.") if {
    some old in data.registry.attributes
    old.stability == "stable"
    current := contract_current_attributes[old.key]
    not object.get(current, "stability", "") in {"stable", "deprecated"}
}

deny contains contract_compatibility_finding("compatibility.metric_removed", old.name,
    "A stable baseline metric was removed or renamed.") if {
    some old in data.registry.metrics
    old.stability == "stable"
    not contract_current_metrics[old.name]
}

deny contains contract_compatibility_finding("compatibility.metric_shape", old.name,
    sprintf("A stable baseline metric changed %s.", [field])) if {
    some old in data.registry.metrics
    old.stability == "stable"
    current := contract_current_metrics[old.name]
    some field in {"unit", "instrument"}
    object.get(old, field, null) != object.get(current, field, null)
}
