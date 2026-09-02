import { describe, expect, test } from "bun:test"

import { ago, called, frame, ms } from "@/lib/time"

describe("milliseconds", () => {
  test("to the precision that tells them apart", () => {
    expect(ms(0.037)).toBe("0.04 ms")
    expect(ms(4.21)).toBe("4.2 ms")
    expect(ms(123.4)).toBe("123 ms")
  })
})

describe("how long ago", () => {
  const now = 1_000_000_000

  test("in the unit that reads", () => {
    expect(ago(now - 1_000, now)).toBe("just now")
    expect(ago(now - 30_000, now)).toBe("30s ago")
    expect(ago(now - 600_000, now)).toBe("10m ago")
    expect(ago(now - 7_200_000, now)).toBe("2h ago")
  })
})

describe("where a statement came from", () => {
  test("the function and the line", () => {
    expect(called("/app/views.py:31 in list_users")).toBe("list_users:31")
  })

  test("the line alone, when the function is the label above it", () => {
    expect(called("/app/tests/test_users.py:31 in test_users", "test_users")).toBe("line 31")
  })

  test("a test in a class carries the class, and the frame does not", () => {
    expect(called("/app/tests/test_users.py:31 in test_one", "TestUsers::test_one")).toBe(
      "line 31",
    )
  })

  test("a frame reads as a name and the end of a path", () => {
    expect(frame("/app/one/two/views.py:31 in list_users")).toEqual({
      name: "list_users",
      where: "two/views.py:31",
    })
  })
})
