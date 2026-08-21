use std::collections::{BTreeMap, HashMap};

use anyhow::{anyhow, bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::parser::{ExtractedCall, ExtractedSymbol, ParseResult};

pub const GENERIC_ANALYSIS_PROFILE: &str = "mainrag.generic-structural.v1";
pub const EXPORT_SCHEMA: &str = "mainrag.storage-v2-intelligence-export.v1";
const DOMAIN_FIELDS: [&str; 4] = ["layer", "side_effect", "resource", "delegation_target"];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StableStructuralCard {
    pub symbol_key: String,
    pub name: String,
    pub qualified_name: String,
    pub symbol_kind: String,
    pub language: String,
    pub signature: Option<String>,
    pub documentation: Option<String>,
    pub visibility: Option<String>,
    pub source_span: SourceSpan,
    pub structure: BTreeMap<String, Value>,
    pub domain: BTreeMap<String, Value>,
    pub field_provenance: BTreeMap<String, FieldProvenance>,
    pub analysis_profile_id: String,
    pub domain_profile: Option<ProfileRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceSpan {
    pub line_start: u32,
    pub line_end: u32,
    pub column_start: u32,
    pub column_end: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileRef {
    pub profile_id: String,
    pub profile_version: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FieldProvenance {
    pub profile_id: String,
    pub profile_version: u64,
    pub rule_id: String,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfileFieldFact {
    pub field: String,
    pub value: String,
    pub rule_id: String,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolvedCall {
    pub caller_symbol_key: String,
    pub callee_symbol_key: String,
    pub callee_name: String,
    pub call_kind: String,
    pub line: u32,
    pub column: u32,
    pub resolution_kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnresolvedCall {
    pub caller_name: String,
    pub callee_name: String,
    pub call_kind: String,
    pub line: u32,
    pub column: u32,
    pub candidate_symbol_keys: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CallResolution {
    pub proven: Vec<ResolvedCall>,
    pub unresolved: Vec<UnresolvedCall>,
}

pub fn stable_symbol_key(item_key: &str, symbol: &ExtractedSymbol) -> Result<String> {
    stable_symbol_key_with_discriminator(item_key, symbol, None)
}

fn stable_symbol_key_with_discriminator(
    item_key: &str,
    symbol: &ExtractedSymbol,
    signature_discriminator: Option<&str>,
) -> Result<String> {
    let qualified = symbol
        .qualified_name
        .as_deref()
        .filter(|value| !value.is_empty())
        .unwrap_or(&symbol.name);
    if item_key.is_empty() || qualified.is_empty() || symbol.language.is_empty() {
        bail!("stable symbol identity requires item, language, and qualified name");
    }
    let kind = symbol.symbol_type.to_string();
    let digest = hash_parts(
        "mainrag.symbol-key.v1",
        [
            item_key,
            symbol.language.as_str(),
            kind.as_str(),
            qualified,
            signature_discriminator.unwrap_or(""),
        ],
    );
    Ok(format!("symbol-v1:{}", hex::encode(digest)))
}

pub fn generic_structural_cards(
    item_key: &str,
    parsed: &ParseResult,
) -> Result<Vec<StableStructuralCard>> {
    let mut cards = Vec::with_capacity(parsed.symbols.len());
    let mut identity_counts = BTreeMap::<String, usize>::new();
    for symbol in &parsed.symbols {
        *identity_counts
            .entry(stable_symbol_key(item_key, symbol)?)
            .or_default() += 1;
    }
    let mut seen = BTreeMap::<String, String>::new();
    for symbol in &parsed.symbols {
        let base_key = stable_symbol_key(item_key, symbol)?;
        let discriminator = if identity_counts[&base_key] > 1 {
            Some(normalized_signature(symbol)?)
        } else {
            None
        };
        let key = stable_symbol_key_with_discriminator(item_key, symbol, discriminator.as_deref())?;
        if let Some(previous) = seen.insert(key.clone(), discriminator.clone().unwrap_or_default())
        {
            bail!("parser output has duplicate stable symbol identity: {previous}");
        }
        cards.push(generic_structural_card_with_key(item_key, symbol, key)?);
    }
    cards.sort_by(|left, right| left.symbol_key.cmp(&right.symbol_key));
    Ok(cards)
}

pub fn generic_structural_card(
    item_key: &str,
    symbol: &ExtractedSymbol,
) -> Result<StableStructuralCard> {
    let symbol_key = stable_symbol_key(item_key, symbol)?;
    generic_structural_card_with_key(item_key, symbol, symbol_key)
}

fn generic_structural_card_with_key(
    item_key: &str,
    symbol: &ExtractedSymbol,
    symbol_key: String,
) -> Result<StableStructuralCard> {
    let qualified_name = symbol
        .qualified_name
        .clone()
        .unwrap_or_else(|| symbol.name.clone());
    let mut structure = BTreeMap::new();
    structure.insert("item_key".to_string(), Value::String(item_key.to_string()));
    structure.insert(
        "has_documentation".to_string(),
        Value::Bool(symbol.doc_comment.is_some()),
    );
    let domain = DOMAIN_FIELDS
        .into_iter()
        .map(|field| (field.to_string(), Value::String("unknown".to_string())))
        .collect();
    Ok(StableStructuralCard {
        symbol_key,
        name: symbol.name.clone(),
        qualified_name,
        symbol_kind: symbol.symbol_type.to_string(),
        language: symbol.language.clone(),
        signature: symbol.signature.clone(),
        documentation: symbol.doc_comment.clone(),
        visibility: symbol.visibility.clone(),
        source_span: SourceSpan {
            line_start: symbol.line_start,
            line_end: symbol.line_end,
            column_start: symbol.column_start,
            column_end: symbol.column_end,
        },
        structure,
        domain,
        field_provenance: BTreeMap::new(),
        analysis_profile_id: GENERIC_ANALYSIS_PROFILE.to_string(),
        domain_profile: None,
    })
}

fn normalized_signature(symbol: &ExtractedSymbol) -> Result<String> {
    let signature = symbol
        .signature
        .as_deref()
        .ok_or_else(|| anyhow!("overloaded symbol requires a signature discriminator"))?;
    let normalized: String = signature
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect();
    if normalized.is_empty() {
        bail!("overloaded symbol requires a non-empty signature discriminator");
    }
    Ok(normalized)
}

pub fn apply_profile_facts(
    card: &StableStructuralCard,
    profile: &ProfileRef,
    facts: &[ProfileFieldFact],
) -> Result<StableStructuralCard> {
    if profile.profile_id.is_empty() || profile.profile_version == 0 {
        bail!("domain profile id and version are required");
    }
    let mut derived = card.clone();
    derived.domain_profile = Some(profile.clone());
    derived.analysis_profile_id = format!(
        "{GENERIC_ANALYSIS_PROFILE}/{}@{}",
        profile.profile_id, profile.profile_version
    );
    let mut seen_fields = BTreeMap::<&str, ()>::new();
    for fact in facts {
        if !derived.domain.contains_key(&fact.field) {
            bail!("unsupported domain field {}", fact.field);
        }
        if seen_fields.insert(fact.field.as_str(), ()).is_some() {
            bail!("duplicate domain fact for {}", fact.field);
        }
        if fact.value.is_empty()
            || fact.value == "unknown"
            || fact.rule_id.is_empty()
            || fact.evidence.is_empty()
        {
            bail!("domain facts require a value, rule, and evidence");
        }
        derived
            .domain
            .insert(fact.field.clone(), Value::String(fact.value.clone()));
        derived.field_provenance.insert(
            fact.field.clone(),
            FieldProvenance {
                profile_id: profile.profile_id.clone(),
                profile_version: profile.profile_version,
                rule_id: fact.rule_id.clone(),
                evidence: fact.evidence.clone(),
            },
        );
    }
    validate_card(&derived)?;
    Ok(derived)
}

pub fn validate_card(card: &StableStructuralCard) -> Result<()> {
    if card.domain.len() != DOMAIN_FIELDS.len()
        || !DOMAIN_FIELDS
            .iter()
            .all(|field| card.domain.contains_key(*field))
    {
        bail!("card must contain exactly the supported domain fields");
    }
    if card
        .field_provenance
        .keys()
        .any(|field| !card.domain.contains_key(field))
    {
        bail!("field provenance contains an unsupported domain field");
    }
    for (field, value) in &card.domain {
        let text = value
            .as_str()
            .ok_or_else(|| anyhow!("domain field {field} must be a string"))?;
        let is_unknown = text == "unknown";
        if is_unknown && card.field_provenance.contains_key(field) {
            bail!("unknown domain field {field} must not claim provenance");
        }
        if !is_unknown {
            let profile = card
                .domain_profile
                .as_ref()
                .ok_or_else(|| anyhow!("domain field {field} lacks a profile"))?;
            let provenance = card
                .field_provenance
                .get(field)
                .ok_or_else(|| anyhow!("domain field {field} lacks provenance"))?;
            if provenance.profile_id != profile.profile_id
                || provenance.profile_version != profile.profile_version
                || provenance.rule_id.is_empty()
                || provenance.evidence.is_empty()
            {
                bail!("domain field {field} has mismatched provenance");
            }
        }
    }
    Ok(())
}

pub fn normalized_output(card: &StableStructuralCard) -> Result<Vec<u8>> {
    validate_card(card)?;
    serde_json::to_vec(card).map_err(Into::into)
}

pub fn normalized_output_sha256(card: &StableStructuralCard) -> Result<[u8; 32]> {
    Ok(Sha256::digest(normalized_output(card)?).into())
}

pub fn resolve_calls(calls: &[ExtractedCall], cards: &[StableStructuralCard]) -> CallResolution {
    let mut by_name: HashMap<&str, Vec<&StableStructuralCard>> = HashMap::new();
    let mut callers: HashMap<&str, Vec<&StableStructuralCard>> = HashMap::new();
    for card in cards {
        by_name.entry(&card.qualified_name).or_default().push(card);
        callers.entry(&card.name).or_default().push(card);
        if card.qualified_name != card.name {
            callers.entry(&card.qualified_name).or_default().push(card);
        }
    }
    let mut proven = Vec::new();
    let mut unresolved = Vec::new();
    for call in calls {
        let caller = callers
            .get(call.caller_name.as_str())
            .filter(|values| values.len() == 1)
            .and_then(|values| values.first())
            .copied();
        let candidates = by_name
            .get(call.callee_name.as_str())
            .cloned()
            .unwrap_or_default();
        if let (Some(caller), [callee]) = (caller, candidates.as_slice()) {
            proven.push(ResolvedCall {
                caller_symbol_key: caller.symbol_key.clone(),
                callee_symbol_key: callee.symbol_key.clone(),
                callee_name: call.callee_name.clone(),
                call_kind: call.call_type.to_string(),
                line: call.call_line,
                column: call.call_column,
                resolution_kind: "qualified_unique".to_string(),
            });
        } else {
            unresolved.push(UnresolvedCall {
                caller_name: call.caller_name.clone(),
                callee_name: call.callee_name.clone(),
                call_kind: call.call_type.to_string(),
                line: call.call_line,
                column: call.call_column,
                candidate_symbol_keys: candidates
                    .into_iter()
                    .map(|card| card.symbol_key.clone())
                    .collect(),
            });
        }
    }
    proven.sort_by(|left, right| {
        (&left.caller_symbol_key, left.line, left.column).cmp(&(
            &right.caller_symbol_key,
            right.line,
            right.column,
        ))
    });
    unresolved.sort_by(|left, right| {
        (&left.caller_name, left.line, left.column).cmp(&(
            &right.caller_name,
            right.line,
            right.column,
        ))
    });
    CallResolution { proven, unresolved }
}

fn hash_parts<'a>(domain: &str, parts: impl IntoIterator<Item = &'a str>) -> [u8; 32] {
    let parts: Vec<&str> = parts.into_iter().collect();
    let mut digest = Sha256::new();
    digest.update((domain.len() as u64).to_be_bytes());
    digest.update(domain.as_bytes());
    digest.update((parts.len() as u64).to_be_bytes());
    for part in parts {
        digest.update((part.len() as u64).to_be_bytes());
        digest.update(part.as_bytes());
    }
    digest.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::parser::{CallType, SymbolType};

    fn symbol(name: &str, qualified: Option<&str>) -> ExtractedSymbol {
        ExtractedSymbol {
            name: name.to_string(),
            qualified_name: qualified.map(str::to_string),
            symbol_type: SymbolType::Function,
            line_start: 1,
            line_end: 2,
            column_start: 0,
            column_end: 1,
            signature: Some(format!("fn {name}()")),
            doc_comment: Some("synthetic documentation".to_string()),
            visibility: Some("public".to_string()),
            language: "rust".to_string(),
        }
    }

    #[test]
    fn stable_identity_ignores_version_local_spans_and_signatures() {
        let original = symbol("alpha", Some("crate::alpha"));
        let mut changed = original.clone();
        changed.line_start = 80;
        changed.line_end = 92;
        changed.signature = Some("fn alpha(value: usize)".to_string());
        assert_eq!(
            stable_symbol_key("src/lib.rs", &original).unwrap(),
            stable_symbol_key("src/lib.rs", &changed).unwrap()
        );
    }

    #[test]
    fn generic_card_uses_unknown_without_a_profile() {
        let card = generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        assert!(card.domain.values().all(|value| value == "unknown"));
        assert!(card.field_provenance.is_empty());
        assert!(card.domain_profile.is_none());
    }

    #[test]
    fn profile_facts_require_field_level_provenance() {
        let card = generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        let profile = ProfileRef {
            profile_id: "fixture".to_string(),
            profile_version: 2,
        };
        let derived = apply_profile_facts(
            &card,
            &profile,
            &[ProfileFieldFact {
                field: "layer".to_string(),
                value: "api".to_string(),
                rule_id: "public-api".to_string(),
                evidence: "visibility=public".to_string(),
            }],
        )
        .unwrap();
        assert_eq!(derived.domain["layer"], "api");
        assert_eq!(derived.field_provenance["layer"].profile_version, 2);
        assert!(derived.domain["resource"] == "unknown");
    }

    #[test]
    fn unsupported_or_unproven_domain_facts_fail_closed() {
        let card = generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        let profile = ProfileRef {
            profile_id: "fixture".to_string(),
            profile_version: 1,
        };
        assert!(apply_profile_facts(
            &card,
            &profile,
            &[ProfileFieldFact {
                field: "resource".to_string(),
                value: "clips".to_string(),
                rule_id: String::new(),
                evidence: String::new(),
            }]
        )
        .is_err());
        let mut unsupported = card;
        unsupported
            .domain
            .insert("invented".to_string(), Value::String("guess".to_string()));
        assert!(validate_card(&unsupported).is_err());

        let mut typed = generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        typed
            .domain
            .insert("resource".to_string(), Value::Bool(true));
        assert!(validate_card(&typed).is_err());

        let mut unknown_with_provenance =
            generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        unknown_with_provenance.field_provenance.insert(
            "resource".to_string(),
            FieldProvenance {
                profile_id: "fixture".to_string(),
                profile_version: 1,
                rule_id: "guess".to_string(),
                evidence: "none".to_string(),
            },
        );
        assert!(validate_card(&unknown_with_provenance).is_err());

        assert!(apply_profile_facts(
            &generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap(),
            &profile,
            &[
                ProfileFieldFact {
                    field: "layer".to_string(),
                    value: "api".to_string(),
                    rule_id: "rule-a".to_string(),
                    evidence: "evidence-a".to_string(),
                },
                ProfileFieldFact {
                    field: "layer".to_string(),
                    value: "service".to_string(),
                    rule_id: "rule-b".to_string(),
                    evidence: "evidence-b".to_string(),
                },
            ],
        )
        .is_err());
    }

    #[test]
    fn normalized_output_is_byte_identical() {
        let card = generic_structural_card("src/lib.rs", &symbol("alpha", None)).unwrap();
        assert_eq!(
            normalized_output(&card).unwrap(),
            normalized_output(&card).unwrap()
        );
        assert_eq!(
            normalized_output_sha256(&card).unwrap(),
            normalized_output_sha256(&card).unwrap()
        );
    }

    #[test]
    fn exact_qualified_unique_call_is_proven() {
        let parsed = ParseResult {
            symbols: vec![
                symbol("alpha", Some("crate::alpha")),
                symbol("beta", Some("crate::beta")),
            ],
            calls: vec![ExtractedCall {
                caller_name: "alpha".to_string(),
                callee_name: "crate::beta".to_string(),
                call_type: CallType::Direct,
                call_line: 2,
                call_column: 4,
            }],
            language: "rust".to_string(),
        };
        let cards = generic_structural_cards("src/lib.rs", &parsed).unwrap();
        let calls = resolve_calls(&parsed.calls, &cards);
        assert_eq!(calls.proven.len(), 1);
        assert!(calls.unresolved.is_empty());
    }

    #[test]
    fn name_only_or_ambiguous_calls_remain_unresolved() {
        let parsed = ParseResult {
            symbols: vec![
                symbol("alpha", Some("crate::alpha")),
                symbol("beta", Some("one::beta")),
                symbol("beta", Some("two::beta")),
            ],
            calls: vec![ExtractedCall {
                caller_name: "alpha".to_string(),
                callee_name: "beta".to_string(),
                call_type: CallType::Direct,
                call_line: 2,
                call_column: 4,
            }],
            language: "rust".to_string(),
        };
        let cards = generic_structural_cards("src/lib.rs", &parsed).unwrap();
        let calls = resolve_calls(&parsed.calls, &cards);
        assert!(calls.proven.is_empty());
        assert_eq!(calls.unresolved.len(), 1);
    }

    #[test]
    fn ambiguous_stable_identity_is_rejected() {
        let duplicate = symbol("alpha", Some("crate::alpha"));
        let parsed = ParseResult {
            symbols: vec![duplicate.clone(), duplicate],
            calls: vec![],
            language: "rust".to_string(),
        };
        assert!(generic_structural_cards("src/lib.rs", &parsed).is_err());
    }

    #[test]
    fn overloaded_symbols_use_normalized_signature_discriminators() {
        let mut integer = symbol("parse", Some("crate::parse"));
        integer.signature = Some("fn parse(value: i32)".to_string());
        let mut text = integer.clone();
        text.signature = Some("fn parse( value: &str )".to_string());
        let parsed = ParseResult {
            symbols: vec![integer, text],
            calls: vec![],
            language: "rust".to_string(),
        };
        let cards = generic_structural_cards("src/lib.rs", &parsed).unwrap();
        assert_eq!(cards.len(), 2);
        assert_ne!(cards[0].symbol_key, cards[1].symbol_key);
    }
}
