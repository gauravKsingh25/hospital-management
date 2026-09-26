/**
 * How many rows a board shows at once.
 *
 * Deliberately in its own module rather than exported from the board
 * components. Those carry `"use client"`, and a value imported from a client
 * module into a server component is not the value — it is a client-reference
 * stub. Interpolating one into a URL produces a query string containing the
 * text of an error message, which is exactly the bug this file exists to stop
 * happening twice. (Next says so plainly when it happens; it is worth
 * remembering that it happens at all.)
 *
 * The number itself matters less than the fact that the server-rendered first
 * page and the client's first page use *the same* one. When they differed —
 * 50 from the API's default, 25 from the client — the rows past the client's
 * limit vanished from the first paint the moment it refetched, which reads as
 * data disappearing.
 */
export const BOARD_PAGE_SIZE = 25;
