-- Closed block-local vector codec. No source rows, readers, indexes or existing
-- contracts change here. Actual post-TOAST storage savings require admission.
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='30s';
DO $codec_environment$
BEGIN
 -- convert_to/from are generally STABLE because encoding conversions can be
 -- catalog-dependent. This codec uses identical UTF8 source/destination in a
 -- fixed UTF8 database; no conversion lookup or text-search dictionary runs.
 IF getdatabaseencoding()<>'UTF8' THEN
  RAISE EXCEPTION 'lexical vector codec requires a UTF8 database';
 END IF;
 IF EXISTS(SELECT 1 FROM unnest(ARRAY['mainrag','mainrag_v2_lexical_rank_owner',
    'mainrag_v2_frontier_owner','mainrag_v2_presence_owner']) name
    WHERE to_regrole(name) IS NULL) THEN
  RAISE EXCEPTION 'lexical vector codec requires the existing reader/writer roles';
 END IF;
END $codec_environment$;

CREATE FUNCTION public.storage_v2_lexical_codec_u32(p_bytes BYTEA,p_offset INTEGER)
RETURNS INTEGER LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $u32$
DECLARE value BIGINT;
BEGIN
 IF p_offset<0 OR p_offset::BIGINT+4>octet_length(p_bytes) THEN
  RAISE EXCEPTION 'lexical codec truncated integer' USING ERRCODE='22023';
 END IF;
 value:=get_byte(p_bytes,p_offset)::BIGINT*16777216
       +get_byte(p_bytes,p_offset+1)::BIGINT*65536
       +get_byte(p_bytes,p_offset+2)::BIGINT*256+get_byte(p_bytes,p_offset+3);
 IF value>2147483647 THEN
  RAISE EXCEPTION 'lexical codec integer overflow' USING ERRCODE='22023';
 END IF;
 RETURN value::INTEGER;
END $u32$;

-- MLV1, u32 dictionary count, u32 vector count, then dictionary entries
-- (u16 UTF8 length, full bytes), followed by vectors (u32 entry count; each
-- entry: u32 one-based dictionary ID, u16 position count, u16 weight/position).
-- All integers are network byte order. Dictionary order is exact byte order.
-- Optional codec admission: <=64 vectors and <=8MiB expanded vector wire cost.
-- Inputs outside this envelope must retain their existing representation.
CREATE FUNCTION public.storage_v2_encode_lexical_vectors(p_vectors TSVECTOR[])
RETURNS BYTEA LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $encode$
DECLARE encoded BYTEA; wire_bytes BIGINT; expanded_bytes BIGINT;
BEGIN
 IF cardinality(p_vectors)>64 OR (cardinality(p_vectors)>0 AND
     (array_ndims(p_vectors)<>1 OR array_lower(p_vectors,1)<>1))
    THEN
  RAISE EXCEPTION 'lexical codec requires a bounded nonnull one-based vector array' USING ERRCODE='22023';
 END IF;
 IF array_position(p_vectors,NULL) IS NOT NULL THEN
  RAISE EXCEPTION 'lexical codec requires nonnull vector elements' USING ERRCODE='22023';
 END IF;
 -- Array elements are flat vector datums after array deTOAST. Count their
 -- full internal sizes BEFORE relational lexeme/position expansion. Internal
 -- WordEntry/header/alignment costs conservatively cover the UTF8 wire size.
 -- tsvectorsend is unsuitable here: it uses session client_encoding.
 SELECT coalesce(sum(pg_column_size(v)::BIGINT),0) INTO expanded_bytes FROM unnest(p_vectors) v;
 IF expanded_bytes>8388608 THEN
  RAISE EXCEPTION 'lexical codec expanded input admission exceeded' USING ERRCODE='54000';
 END IF;
 WITH vectors AS MATERIALIZED (
  SELECT vector,ordinal::INTEGER AS vector_order
   FROM unnest(p_vectors) WITH ORDINALITY input(vector,ordinal)
 ), entries AS MATERIALIZED (
  SELECT v.vector_order,e.lexeme,convert_to(e.lexeme,'UTF8') AS lexeme_bytes,
         e.positions,e.weights
   FROM vectors v CROSS JOIN LATERAL unnest(v.vector) e
 ), dictionary AS MATERIALIZED (
  SELECT lexeme_bytes,row_number() OVER(ORDER BY lexeme_bytes)::INTEGER AS dictionary_id
   FROM (SELECT DISTINCT lexeme_bytes FROM entries) unique_terms
 ), encoded_entries AS MATERIALIZED (
  SELECT e.vector_order,d.dictionary_id,
   int4send(d.dictionary_id)||int2send(coalesce(cardinality(e.positions),0)::SMALLINT)
    ||coalesce((SELECT string_agg(int2send((CASE WHEN bits>32767 THEN bits-65536 ELSE bits END)::SMALLINT),
            decode('','hex') ORDER BY ordinal)
       FROM (SELECT ordinal,term_position::INTEGER + CASE weight
         WHEN 'A' THEN 49152 WHEN 'B' THEN 32768 WHEN 'C' THEN 16384 ELSE 0 END AS bits
         FROM unnest(e.positions,e.weights) WITH ORDINALITY p(term_position,weight,ordinal)) words),
        decode('','hex')) AS encoded_entry
   FROM entries e JOIN dictionary d USING(lexeme_bytes)
 ), encoded_vectors AS MATERIALIZED (
  SELECT v.vector_order,int4send(count(e.dictionary_id)::INTEGER)
     ||coalesce(string_agg(e.encoded_entry,decode('','hex') ORDER BY e.dictionary_id),decode('','hex')) AS encoded_vector
   FROM vectors v LEFT JOIN encoded_entries e USING(vector_order)
   GROUP BY v.vector_order
 ) SELECT decode('4d4c5631','hex')||int4send((SELECT count(*)::INTEGER FROM dictionary))
     ||int4send(cardinality(p_vectors))
     ||coalesce((SELECT string_agg(int2send(octet_length(lexeme_bytes)::SMALLINT)||lexeme_bytes,
                     decode('','hex') ORDER BY dictionary_id) FROM dictionary),decode('','hex'))
     ||coalesce((SELECT string_agg(encoded_vector,decode('','hex') ORDER BY vector_order)
                    FROM encoded_vectors),decode('','hex')),
   cardinality(p_vectors)::BIGINT*4+(SELECT coalesce(sum(octet_length(lexeme_bytes)::BIGINT+3
                              +2*coalesce(cardinality(positions),0)),0) FROM entries)
  INTO encoded,wire_bytes;
 IF wire_bytes>8388608 OR octet_length(encoded)>8388608 THEN
  RAISE EXCEPTION 'lexical codec payload admission exceeded' USING ERRCODE='54000';
 END IF;
 RETURN encoded;
END $encode$;

CREATE FUNCTION public.storage_v2_decode_lexical_vectors(p_encoded BYTEA)
RETURNS TSVECTOR[] LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $decode$
DECLARE dictionary_count INTEGER; vector_count INTEGER; bytes BYTEA; offset_idx INTEGER:=12;
 size INTEGER:=octet_length(p_encoded); dictionary TEXT[]:=ARRAY[]::TEXT[];
 dictionary_lengths INTEGER[]:=ARRAY[]::INTEGER[]; used_terms BOOLEAN[]; previous_bytes BYTEA; lexeme_bytes BYTEA;
 lexeme_length INTEGER; dictionary_id INTEGER; previous_id INTEGER; entries INTEGER;
 positions INTEGER; term_position INTEGER; previous_position INTEGER; bits INTEGER;
 vector_number INTEGER; entry_number INTEGER; i INTEGER;
 lexeme TEXT; quoted TEXT; position_items TEXT[]; vector_items TEXT[];
 decoded TSVECTOR[]:=ARRAY[]::TSVECTOR[]; wire_bytes BIGINT:=0;
BEGIN
 IF size<12 OR size>8388608 THEN
  RAISE EXCEPTION 'lexical codec invalid header or payload size' USING ERRCODE='22023';
 END IF;
 -- Decode a toasted/compressed parameter once. Repeated get_byte on an
 -- external parameter would otherwise fetch/decompress it for every word.
 bytes:=p_encoded||decode('','hex');
 IF substring(bytes FROM 1 FOR 4)<>decode('4d4c5631','hex') THEN
  RAISE EXCEPTION 'lexical codec invalid header' USING ERRCODE='22023';
 END IF;
 dictionary_count:=public.storage_v2_lexical_codec_u32(bytes,4);
 vector_count:=public.storage_v2_lexical_codec_u32(bytes,8);
 IF vector_count>64 OR dictionary_count>(size-12)/3 THEN
  RAISE EXCEPTION 'lexical codec invalid dictionary or vector count' USING ERRCODE='22023';
 END IF;
 used_terms:=array_fill(FALSE,ARRAY[dictionary_count]);
 FOR i IN 1..dictionary_count LOOP
  IF offset_idx::BIGINT+2>size THEN
   RAISE EXCEPTION 'lexical codec truncated dictionary length' USING ERRCODE='22023';
  END IF;
  lexeme_length:=get_byte(bytes,offset_idx)*256+get_byte(bytes,offset_idx+1);offset_idx:=offset_idx+2;
  IF lexeme_length NOT BETWEEN 1 AND 2046 OR offset_idx::BIGINT+lexeme_length>size THEN
   RAISE EXCEPTION 'lexical codec invalid dictionary length' USING ERRCODE='22023';
  END IF;
  lexeme_bytes:=substring(bytes FROM offset_idx+1 FOR lexeme_length);offset_idx:=offset_idx+lexeme_length;
  IF previous_bytes IS NOT NULL AND lexeme_bytes<=previous_bytes THEN
   RAISE EXCEPTION 'lexical codec dictionary is not unique byte order' USING ERRCODE='22023';
  END IF;
  -- Invalid UTF8 or NUL bytes fail convert_from, rather than being normalized.
  lexeme:=convert_from(lexeme_bytes,'UTF8');previous_bytes:=lexeme_bytes;
  dictionary:=array_append(dictionary,lexeme);
  dictionary_lengths:=array_append(dictionary_lengths,lexeme_length);
 END LOOP;
 FOR vector_number IN 1..vector_count LOOP
  entries:=public.storage_v2_lexical_codec_u32(bytes,offset_idx);offset_idx:=offset_idx+4;
  IF entries>dictionary_count OR entries::BIGINT*6>size-offset_idx THEN
   RAISE EXCEPTION 'lexical codec invalid vector entry count' USING ERRCODE='22023';
  END IF;
  vector_items:=ARRAY[]::TEXT[];previous_id:=0;wire_bytes:=wire_bytes+4;
  FOR entry_number IN 1..entries LOOP
   dictionary_id:=public.storage_v2_lexical_codec_u32(bytes,offset_idx);offset_idx:=offset_idx+4;
   IF dictionary_id<=previous_id OR dictionary_id>dictionary_count THEN
    RAISE EXCEPTION 'lexical codec invalid dictionary reference' USING ERRCODE='22023';
   END IF;
   previous_id:=dictionary_id;used_terms[dictionary_id]:=TRUE;
   IF offset_idx::BIGINT+2>size THEN
    RAISE EXCEPTION 'lexical codec truncated position count' USING ERRCODE='22023';
   END IF;
   positions:=get_byte(bytes,offset_idx)*256+get_byte(bytes,offset_idx+1);offset_idx:=offset_idx+2;
   IF positions>256 OR offset_idx::BIGINT+positions*2>size THEN
    RAISE EXCEPTION 'lexical codec invalid position count' USING ERRCODE='22023';
   END IF;
   wire_bytes:=wire_bytes+dictionary_lengths[dictionary_id]+3+positions*2;
   IF wire_bytes>8388608 THEN
    RAISE EXCEPTION 'lexical codec expanded payload admission exceeded' USING ERRCODE='54000';
   END IF;
   -- Match PostgreSQL's tsvector output escaping. This type-input cast parses
   -- serialized lexemes/positions only; it does not run a tokenizer/dictionary.
   quoted:=chr(39)||replace(replace(dictionary[dictionary_id],chr(92),chr(92)||chr(92)),
              chr(39),chr(39)||chr(39))||chr(39);
   position_items:=ARRAY[]::TEXT[];previous_position:=0;
   FOR i IN 1..positions LOOP
    bits:=get_byte(bytes,offset_idx)*256+get_byte(bytes,offset_idx+1);offset_idx:=offset_idx+2;
    term_position:=bits&16383;
    IF term_position<=previous_position THEN
     RAISE EXCEPTION 'lexical codec invalid position order' USING ERRCODE='22023';
    END IF;
    previous_position:=term_position;
    position_items:=array_append(position_items,term_position::TEXT||CASE bits>>14
       WHEN 3 THEN 'A' WHEN 2 THEN 'B' WHEN 1 THEN 'C' ELSE '' END);
   END LOOP;
   IF positions>0 THEN quoted:=quoted||':'||array_to_string(position_items,',');END IF;
   vector_items:=array_append(vector_items,quoted);
  END LOOP;
  decoded:=array_append(decoded,array_to_string(vector_items,' ')::TSVECTOR);
 END LOOP;
 IF offset_idx<>size OR array_position(used_terms,FALSE) IS NOT NULL THEN
  RAISE EXCEPTION 'lexical codec trailing payload or unused dictionary' USING ERRCODE='22023';
 END IF;
 RETURN decoded;
END $decode$;

-- Use the same complete lexeme union semantics as the existing native GIN
-- expression, but with a pure local decoder. No mutable dictionary/table read.
CREATE FUNCTION public.storage_v2_lexical_codec_exact_lexemes(p_encoded BYTEA)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $lexemes$
 SELECT ARRAY(SELECT DISTINCT lexeme COLLATE "C"
  FROM unnest(public.storage_v2_decode_lexical_vectors(p_encoded)) vector
  CROSS JOIN LATERAL unnest(tsvector_to_array(vector)) lexeme ORDER BY 1)
$lexemes$;

ALTER FUNCTION public.storage_v2_lexical_codec_u32(BYTEA,INTEGER) OWNER TO mainrag_v2_lexical_rank_owner;
ALTER FUNCTION public.storage_v2_encode_lexical_vectors(TSVECTOR[]) OWNER TO mainrag_v2_lexical_rank_owner;
ALTER FUNCTION public.storage_v2_decode_lexical_vectors(BYTEA) OWNER TO mainrag_v2_lexical_rank_owner;
ALTER FUNCTION public.storage_v2_lexical_codec_exact_lexemes(BYTEA) OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION public.storage_v2_lexical_codec_u32(BYTEA,INTEGER),
 public.storage_v2_encode_lexical_vectors(TSVECTOR[]),public.storage_v2_decode_lexical_vectors(BYTEA),
 public.storage_v2_lexical_codec_exact_lexemes(BYTEA) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.storage_v2_lexical_codec_u32(BYTEA,INTEGER),
 public.storage_v2_encode_lexical_vectors(TSVECTOR[]),public.storage_v2_decode_lexical_vectors(BYTEA),
 public.storage_v2_lexical_codec_exact_lexemes(BYTEA)
 TO mainrag,mainrag_v2_frontier_owner,mainrag_v2_presence_owner;
COMMENT ON FUNCTION public.storage_v2_encode_lexical_vectors(TSVECTOR[]) IS
 'Optional MLV1 block-local lossless vector encoding; no retokenization. Actual stored compression and decoder costs must be admitted before replacing any retained payload.';
COMMIT;
