//! Exact storage-v2 query parsing and backend-neutral request contract.
//!
//! The production PostgreSQL backend is additive and selected only through an
//! explicit API read selector. Query parsing is shared by every backend so a
//! future qualified implementation cannot silently fork Boolean semantics.

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio_postgres::Transaction;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum QueryAst {
    Term { value: String },
    Phrase { value: String },
    Exact { value: String },
    Not { children: [Box<QueryAst>; 1] },
    Group { children: [Box<QueryAst>; 1] },
    And { children: Vec<QueryAst> },
    Or { children: Vec<QueryAst> },
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    Word(String),
    Phrase(String),
    Exact(String),
    And,
    Or,
    Not,
    LeftParen,
    RightParen,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExactSearchRequest {
    pub source_id: i64,
    pub generation: String,
    pub ast: QueryAst,
    pub filters: Value,
    pub limit: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExactSearchEnvelope {
    pub generation_seq: i64,
    pub execution: String,
    pub fully_scored_views: i64,
    pub total: i64,
    pub results: Vec<ExactSearchHit>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExactSearchHit {
    pub occurrence_id: i64,
    pub external_hit_id: String,
    pub view_id: i64,
    pub source_id: i64,
    pub source_name: String,
    pub source_path: String,
    pub locator: Value,
    pub role: String,
    pub content: String,
    pub score: f64,
    pub score_explanation: Value,
    pub legacy_successors: Vec<LegacySuccessor>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacySuccessor {
    pub old_hit_id: String,
    pub ordinal: i64,
    pub relation_kind: String,
}

/// Backend-neutral boundary. A later qualified backend must consume the same
/// normalized AST, filters, generation selector, and result contract.
#[async_trait::async_trait]
pub trait ExactRetrievalBackend {
    async fn search(&self, request: &ExactSearchRequest) -> Result<ExactSearchEnvelope>;
}

pub struct PostgresExactRetrievalBackend<'a> {
    transaction: &'a Transaction<'a>,
}

impl<'a> PostgresExactRetrievalBackend<'a> {
    pub fn new(transaction: &'a Transaction<'a>) -> Self {
        Self { transaction }
    }
}

#[async_trait::async_trait]
impl ExactRetrievalBackend for PostgresExactRetrievalBackend<'_> {
    async fn search(&self, request: &ExactSearchRequest) -> Result<ExactSearchEnvelope> {
        let ast = serde_json::to_value(&request.ast).context("serialize normalized search AST")?;
        let row = self
            .transaction
            .query_one(
                "SELECT storage_v2_search_exact($1, $2, $3, $4, $5)",
                &[
                    &request.source_id,
                    &request.generation,
                    &ast,
                    &request.filters,
                    &request.limit,
                ],
            )
            .await?;
        let value: Value = row.get(0);
        serde_json::from_value(value).context("decode exact retrieval response")
    }
}

pub fn parse_query(input: &str) -> Result<QueryAst> {
    let tokens = lex(input)?;
    let mut parser = Parser { tokens, index: 0 };
    let ast = parser.parse_or()?;
    if parser.index != parser.tokens.len() {
        bail!("unexpected token at position {}", parser.index);
    }
    if !has_positive_anchor(&ast) {
        bail!("NOT must filter a positively established branch");
    }
    Ok(ast)
}

fn lex(input: &str) -> Result<Vec<Token>> {
    let chars: Vec<char> = input.trim().chars().collect();
    if chars.is_empty() {
        bail!("query is empty");
    }
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        if chars[index].is_whitespace() {
            index += 1;
            continue;
        }
        match chars[index] {
            '(' => {
                tokens.push(Token::LeftParen);
                index += 1;
            }
            ')' => {
                tokens.push(Token::RightParen);
                index += 1;
            }
            '-' => {
                tokens.push(Token::Not);
                index += 1;
            }
            '"' => {
                index += 1;
                let mut phrase = String::new();
                let mut closed = false;
                while index < chars.len() {
                    match chars[index] {
                        '\\' if index + 1 < chars.len() && chars[index + 1] == '"' => {
                            phrase.push('"');
                            index += 2;
                        }
                        '"' => {
                            closed = true;
                            index += 1;
                            break;
                        }
                        character => {
                            phrase.push(character);
                            index += 1;
                        }
                    }
                }
                let normalized = phrase.split_whitespace().collect::<Vec<_>>().join(" ");
                if !closed || normalized.is_empty() {
                    bail!("quoted phrase is empty or unterminated");
                }
                tokens.push(Token::Phrase(normalized.to_lowercase()));
            }
            _ => {
                let start = index;
                while index < chars.len()
                    && !chars[index].is_whitespace()
                    && !matches!(chars[index], '(' | ')' | '"')
                {
                    index += 1;
                }
                let value: String = chars[start..index].iter().collect();
                let upper = value.to_ascii_uppercase();
                match upper.as_str() {
                    "AND" => tokens.push(Token::And),
                    "OR" => tokens.push(Token::Or),
                    "NOT" => tokens.push(Token::Not),
                    _ if value
                        .get(..3)
                        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("id:")) =>
                    {
                        let identifier = value
                            .get(3..)
                            .ok_or_else(|| anyhow!("invalid exact identifier"))?
                            .trim()
                            .to_lowercase();
                        if identifier.is_empty() {
                            bail!("exact identifier is empty");
                        }
                        tokens.push(Token::Exact(identifier));
                    }
                    _ => tokens.push(Token::Word(value.to_lowercase())),
                }
            }
        }
    }
    if tokens.is_empty() {
        bail!("query is empty");
    }
    Ok(tokens)
}

struct Parser {
    tokens: Vec<Token>,
    index: usize,
}

impl Parser {
    fn current(&self) -> Option<&Token> {
        self.tokens.get(self.index)
    }

    fn take(&mut self) -> Result<Token> {
        let token = self
            .tokens
            .get(self.index)
            .cloned()
            .ok_or_else(|| anyhow!("unexpected end of query"))?;
        self.index += 1;
        Ok(token)
    }

    fn parse_or(&mut self) -> Result<QueryAst> {
        let mut children = vec![self.parse_and()?];
        while matches!(self.current(), Some(Token::Or)) {
            self.index += 1;
            children.push(self.parse_and()?);
        }
        if children.len() == 1 {
            Ok(children.remove(0))
        } else {
            Ok(QueryAst::Or { children })
        }
    }

    fn parse_and(&mut self) -> Result<QueryAst> {
        let mut children = vec![self.parse_unary()?];
        loop {
            match self.current() {
                None | Some(Token::Or | Token::RightParen) => break,
                Some(Token::And) => {
                    self.index += 1;
                    children.push(self.parse_unary()?);
                }
                _ => children.push(self.parse_unary()?),
            }
        }
        if children.len() == 1 {
            Ok(children.remove(0))
        } else {
            Ok(QueryAst::And { children })
        }
    }

    fn parse_unary(&mut self) -> Result<QueryAst> {
        match self.take()? {
            Token::Not => Ok(QueryAst::Not {
                children: [Box::new(self.parse_unary()?)],
            }),
            Token::LeftParen => {
                let child = self.parse_or()?;
                if !matches!(self.take()?, Token::RightParen) {
                    bail!("unclosed query group");
                }
                Ok(QueryAst::Group {
                    children: [Box::new(child)],
                })
            }
            Token::Word(value) if !value.is_empty() => Ok(QueryAst::Term { value }),
            Token::Phrase(value) => Ok(QueryAst::Phrase { value }),
            Token::Exact(value) => Ok(QueryAst::Exact { value }),
            Token::And => bail!("AND lacks a left operand"),
            Token::Or => bail!("OR lacks a left operand"),
            Token::RightParen => bail!("unexpected closing parenthesis"),
            Token::Word(_) => bail!("query term is empty"),
        }
    }
}

fn has_positive_anchor(ast: &QueryAst) -> bool {
    match ast {
        QueryAst::Term { .. } | QueryAst::Phrase { .. } | QueryAst::Exact { .. } => true,
        QueryAst::Not { .. } => false,
        QueryAst::Group { children } => has_positive_anchor(&children[0]),
        QueryAst::And { children } => children.iter().any(has_positive_anchor),
        QueryAst::Or { children } => children.iter().all(has_positive_anchor),
    }
}

#[cfg(test)]
fn matches_ast(ast: &QueryAst, terms: &[&str], phrases: &[&str], exact: &[&str]) -> bool {
    match ast {
        QueryAst::Term { value } => terms.contains(&value.as_str()),
        QueryAst::Phrase { value } => phrases.contains(&value.as_str()),
        QueryAst::Exact { value } => exact.contains(&value.as_str()),
        QueryAst::Not { children } => !matches_ast(&children[0], terms, phrases, exact),
        QueryAst::Group { children } => matches_ast(&children[0], terms, phrases, exact),
        QueryAst::And { children } => children
            .iter()
            .all(|child| matches_ast(child, terms, phrases, exact)),
        QueryAst::Or { children } => children
            .iter()
            .any(|child| matches_ast(child, terms, phrases, exact)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn precedence_and_implicit_and_are_deterministic() {
        let explicit = parse_query("alpha AND beta OR gamma").unwrap();
        let implicit = parse_query("alpha beta OR gamma").unwrap();
        assert_eq!(explicit, implicit);
        assert!(matches_ast(&explicit, &["alpha", "beta"], &[], &[]));
        assert!(matches_ast(&explicit, &["gamma"], &[], &[]));
        assert!(!matches_ast(&explicit, &["alpha"], &[], &[]));
    }

    #[test]
    fn group_phrase_exact_and_not_have_one_shared_ast() {
        let ast = parse_query("(alpha OR \"beta gamma\") AND id:Exact_Name NOT decoy").unwrap();
        assert!(matches_ast(&ast, &["alpha"], &[], &["exact_name"]));
        assert!(matches_ast(&ast, &[], &["beta gamma"], &["exact_name"]));
        assert!(!matches_ast(
            &ast,
            &["alpha", "decoy"],
            &[],
            &["exact_name"]
        ));
    }

    #[test]
    fn not_never_creates_an_unanchored_or_universe() {
        for query in [
            "NOT decoy",
            "-decoy",
            "alpha OR NOT decoy",
            "alpha OR -decoy",
        ] {
            assert!(parse_query(query).is_err(), "query must fail: {query}");
        }
        assert!(parse_query("alpha AND NOT decoy").is_ok());
    }

    #[test]
    fn malformed_inputs_fail_closed() {
        for query in [
            "",
            "AND alpha",
            "alpha OR",
            "alpha AND",
            "(",
            "alpha)",
            "(alpha OR beta",
            "\"\"",
            "\"unterminated",
            "id:",
        ] {
            assert!(parse_query(query).is_err(), "query must fail: {query:?}");
        }
    }

    #[test]
    fn normalized_ast_serialization_is_byte_stable() {
        let first = serde_json::to_vec(&parse_query("Alpha and id:Thing_1").unwrap()).unwrap();
        let second = serde_json::to_vec(&parse_query("alpha AND id:thing_1").unwrap()).unwrap();
        assert_eq!(first, second);
    }
}
