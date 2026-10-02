@calls
Feature: Call and member queries stay stable in every language
  Callers and callees follow MCP search hints.
  Siblings come from the expanded CLI search, the only surface that reports them.
  Each fixture puts every call form of its language on a known line.

  Background:
    Given an example workspace

  Scenario: Rust calls and members
    Given the file "calls/sample.rs" contains exactly
      """
      macro_rules! shout {
          () => {};
      }

      pub fn helper() -> u8 {
          1
      }

      pub struct Widget {
          size: u8,
      }

      impl Widget {
          pub fn make() -> Widget {
              Widget { size: 1 }
          }

          pub fn ping(&self) -> u8 {
              self.size
          }

          pub fn run(&self) -> u8 {
              self.ping() + helper() + self.size
          }
      }

      pub fn driver() -> u8 {
          let w = Widget::make();
          shout!();
          w.ping() + helper()
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.rs:23 run
      calls/sample.rs:30 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.rs:23 run
      calls/sample.rs:30 driver
      """
    When I search for "make"
    Then the search callers are exactly
      """
      calls/sample.rs:28 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.rs:14 make
      calls/sample.rs:18 ping
      calls/sample.rs:5 helper
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.rs:18-20
      """

  Scenario: Python calls and members
    Given the file "calls/sample.py" contains exactly
      """
      def helper():
          return 1


      class Widget:
          def __init__(self):
              self.size = 1

          def ping(self):
              return self.size

          def run(self):
              return self.ping() + helper() + self.size


      def driver():
          w = Widget()
          return w.ping() + helper()
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.py:13 Widget.run
      calls/sample.py:18 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.py:13 Widget.run
      calls/sample.py:18 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.py:1 helper
      calls/sample.py:5 Widget
      calls/sample.py:9 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.py:9-10
      """

  Scenario: Go calls and members
    Given the file "calls/sample.go" contains exactly
      """
      package calls

      func helper() int {
      	return 1
      }

      type Widget struct {
      	size int
      }

      func (w *Widget) ping() int {
      	return w.size
      }

      func (w *Widget) run() int {
      	return w.ping() + helper() + w.size
      }

      func driver() int {
      	w := &Widget{}
      	return w.ping() + helper()
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.go:16 run
      calls/sample.go:21 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.go:16 run
      calls/sample.go:21 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.go:11 ping
      calls/sample.go:3 helper
      """

  Scenario: C calls and members
    Given the file "calls/sample.c" contains exactly
      """
      struct widget {
          int (*ping)(void);
      };

      int helper(void) {
          return 1;
      }

      int ping(void) {
          return helper();
      }

      int driver(struct widget *w) {
          return w->ping() + helper();
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.c:10 ping
      calls/sample.c:14 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.c:14 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.c:5 helper
      calls/sample.c:9 ping
      """

  Scenario: C++ calls and members
    Given the file "calls/sample.cpp" contains exactly
      """
      int helper() {
          return 1;
      }

      class Widget {
      public:
          int ping() { return 1; }
          int run() { return this->ping() + helper(); }
      };

      int driver() {
          Widget w;
          return w.ping() + helper();
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.cpp:13 driver
      calls/sample.cpp:8 Widget.run
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.cpp:1 helper
      """

  @f65e7bd @pr301
  Scenario: C# calls and members
    Given the file "calls/Sample.cs" contains exactly
      """
      public class Widget
      {
          private int size = 1;

          public static int Helper()
          {
              return 1;
          }

          public int Ping()
          {
              return this.size;
          }

          public int Run()
          {
              return this.Ping() + Helper() + this.size;
          }
      }

      public class Driver
      {
          public int Drive(Widget w)
          {
              return w.Ping() + Widget.Helper();
          }
      }
      """
    When I search for "Helper"
    Then the search callers are exactly
      """
      calls/Sample.cs:17 Widget.Run
      calls/Sample.cs:25 Driver.Drive
      """
    When I search for "Ping"
    Then the search callers are exactly
      """
      calls/Sample.cs:17 Widget.Run
      calls/Sample.cs:25 Driver.Drive
      """
    When I search for "Drive"
    Then the search callees are exactly
      """
      calls/Sample.cs:10 Ping
      calls/Sample.cs:5 Helper
      """
    When I run the expanded CLI search for "Run"
    Then the CLI siblings section is exactly
      """
      Ping calls/Sample.cs:10-13
      """

  Scenario: Java calls and members
    Given the file "calls/Sample.java" contains exactly
      """
      class Widget {
          private int size = 1;

          static int helper() {
              return 1;
          }

          int ping() {
              return this.size;
          }

          int run() {
              return this.ping() + helper() + this.size;
          }
      }

      class Driver {
          int drive(Widget w) {
              return w.ping() + Widget.helper();
          }
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/Sample.java:13 Widget.run
      calls/Sample.java:19 Driver.drive
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/Sample.java:13 Widget.run
      calls/Sample.java:19 Driver.drive
      """
    When I search for "drive"
    Then the search callees are exactly
      """
      calls/Sample.java:4 helper
      calls/Sample.java:8 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/Sample.java:8-10
      """

  Scenario: JavaScript calls and members
    Given the file "calls/sample.js" contains exactly
      """
      export function helper() {
        return 1;
      }

      export class Widget {
        constructor() {
          this.size = 1;
        }

        ping() {
          return this.size;
        }

        run() {
          return this.ping() + helper() + this.size;
        }
      }

      export function driver() {
        const w = new Widget();
        return w.ping() + helper();
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.js:15 Widget.run
      calls/sample.js:21 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.js:15 Widget.run
      calls/sample.js:21 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.js:1 helper
      calls/sample.js:10 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.js:10-12
      """

  Scenario: TypeScript calls and members
    Given the file "calls/sample.ts" contains exactly
      """
      export function helper(): number {
        return 1;
      }

      export class Widget {
        size = 1;

        ping(): number {
          return this.size;
        }

        run(): number {
          return this.ping() + helper() + this.size;
        }
      }

      export function driver(): number {
        const w = new Widget();
        return w.ping() + helper();
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.ts:13 Widget.run
      calls/sample.ts:19 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.ts:13 Widget.run
      calls/sample.ts:19 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.ts:1 helper
      calls/sample.ts:8 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.ts:8-10
      """

  Scenario: TSX calls and members
    Given the file "calls/sample.tsx" contains exactly
      """
      export function helper(): number {
        return 1;
      }

      export class Widget {
        size = 1;

        ping(): number {
          return this.size;
        }

        run() {
          return <div>{this.ping() + helper() + this.size}</div>;
        }
      }

      export function driver() {
        const w = new Widget();
        return <span>{w.ping() + helper()}</span>;
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.tsx:13 Widget.run
      calls/sample.tsx:19 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.tsx:13 Widget.run
      calls/sample.tsx:19 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.tsx:1 helper
      calls/sample.tsx:8 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.tsx:8-10
      """

  Scenario: Kotlin calls and members
    Given the file "calls/sample.kt" contains exactly
      """
      fun helper(): Int = 1

      class Widget {
          val size = 1

          fun ping(): Int = size

          fun run(): Int = this.ping() + helper() + size
      }

      fun driver(): Int {
          val w = Widget()
          return w.ping() + helper()
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.kt:13 driver
      calls/sample.kt:8 Widget.run
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.kt:13 driver
      calls/sample.kt:8 Widget.run
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.kt:1 helper
      calls/sample.kt:3 Widget
      calls/sample.kt:6 ping
      """

  Scenario: PHP calls and members
    Known gap: callers bind only the bare name, so the qualified calls on lines 22 and 23 are not callers of `helper`.
    Given the file "calls/sample.php" contains exactly
      """
      <?php
      namespace calls;

      function helper(): int {
          return 1;
      }

      class Widget {
          public function ping(): int {
              return 1;
          }

          public static function make(): Widget {
              return new Widget();
          }
      }

      function driver(?Widget $w): int {
          $made = Widget::make();
          $a = $w?->ping();
          $b = $made->ping();
          $c = \calls\helper();
          $d = namespace\helper();
          return helper();
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.php:24 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.php:20 driver
      calls/sample.php:21 driver
      """
    When I search for "make"
    Then the search callers are exactly
      """
      calls/sample.php:19 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.php:13 make
      calls/sample.php:4 helper
      calls/sample.php:9 ping
      """

  Scenario: Ruby calls and members
    Given the file "calls/sample.rb" contains exactly
      """
      def helper
        1
      end

      class Widget
        def ping
          1
        end

        def run
          ping + helper()
        end
      end

      def driver
        w = Widget.new
        w.ping + helper()
      end
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.rb:11 Widget.run
      calls/sample.rb:17 driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.rb:17 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.rb:1 helper
      calls/sample.rb:6 ping
      """

  Scenario: Scala calls and members
    Given the file "calls/sample.scala" contains exactly
      """
      def helper(): Int = 1

      class Widget {
        val size = 1
        def ping(): Int = size
        def plus(other: Int): Int = other
        def run(): Int = this.ping() + helper() + this.size
      }

      def driver(w: Widget): Int = w.ping() + helper() + (w plus 1)
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.scala:10 driver
      calls/sample.scala:7 Widget.run
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.scala:10 driver
      calls/sample.scala:7 Widget.run
      """
    When I search for "plus"
    Then the search callers are exactly
      """
      calls/sample.scala:10 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.scala:1 helper
      calls/sample.scala:5 ping
      calls/sample.scala:6 plus
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.scala:5-5
      size calls/sample.scala:4-4
      """

  Scenario: Swift calls and members
    Given the file "calls/sample.swift" contains exactly
      """
      func helper() -> Int {
          return 1
      }

      class Widget {
          var size = 1

          func ping() -> Int {
              return size
          }

          func run() -> Int {
              return self.ping()
          }
      }

      func driver(w: Widget) -> Int {
          return w.ping()
      }

      func plain() -> Int {
          return helper()
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.swift:22 plain
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.swift:13 Widget.run
      calls/sample.swift:18 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.swift:8 ping
      """
    When I run the expanded CLI search for "run"
    Then the CLI siblings section is exactly
      """
      ping calls/sample.swift:8-10
      """

  Scenario: Elixir calls and members
    Given the file "calls/sample.ex" contains exactly
      """
      defmodule Calls do
        def helper, do: 1

        def ping(x), do: x

        def driver do
          Calls.ping(helper())
        end
      end
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.ex:7 Calls.driver
      """
    When I search for "ping"
    Then the search callers are exactly
      """
      calls/sample.ex:7 Calls.driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.ex:2 helper
      calls/sample.ex:4 ping
      """

  Scenario: Bash calls and members
    Given the file "calls/sample.sh" contains exactly
      """
      helper() {
        echo one
      }

      driver() {
        helper
        echo done
      }
      """
    When I search for "helper"
    Then the search callers are exactly
      """
      calls/sample.sh:6 driver
      """
    When I search for "driver"
    Then the search callees are exactly
      """
      calls/sample.sh:1 helper
      """
