/* ============================================================================
 * MODULE 00 — SHARED REACT RUNTIME
 * Purpose: establish the React hooks used by all following UI modules.
 * Load order: must be the first Babel module, before every page/component file.
 * Contract: later files share this classic-script lexical scope; do not redeclare
 * these names in another module. This file intentionally contains no UI.
 * ========================================================================== */
const { useState, useEffect, useMemo, useRef, useCallback } = React;
