@aliases @bc61504 @pr137
Feature: Every registered language filename works through MCP
  The matrix is independent of the production language registry.
  Each row searches, reads, tags, edits, and searches the same isolated file.

  Background:
    Given an example workspace

  Scenario Outline: <language> source in <path>
    Given a "<language>" fixture at "<path>"
    When I search for the fixture marker
    Then the search resolves the fixture marker
    When I read the fixture
    Then the read returns the complete fixture source
    When I replace the fixture marker using the read tag
    Then the edit is applied
    And the fixture file contains the complete edited source
    When I search for the updated fixture marker
    Then the search resolves the updated fixture marker
    And the original fixture marker has no matches

    Examples: grammar-backed extension aliases
      | language   | path                    |
      | Rust       | aliases/marker.rs       |
      | TypeScript | aliases/marker.ts       |
      | TSX        | aliases/marker.tsx      |
      | JavaScript | aliases/marker.js       |
      | JavaScript | aliases/marker.jsx      |
      | Python     | aliases/marker.py       |
      | Python     | aliases/marker.pyi      |
      | Go         | aliases/marker.go       |
      | Java       | aliases/Marker.java     |
      | Scala      | aliases/marker.scala    |
      | Scala      | aliases/marker.sc       |
      | C          | aliases/marker.c        |
      | C          | aliases/marker.h        |
      | C++        | aliases/marker.cpp      |
      | C++        | aliases/marker.hpp      |
      | C++        | aliases/marker.cc       |
      | C++        | aliases/marker.cxx      |
      | Ruby       | aliases/marker.rb       |
      | PHP        | aliases/marker.php      |
      | PHP        | aliases/marker.phtml    |
      | Swift      | aliases/marker.swift    |
      | Kotlin     | aliases/marker.kt       |
      | Kotlin     | aliases/marker.kts      |
      | C#         | aliases/marker.cs       |
      | Elixir     | aliases/marker.ex       |
      | Elixir     | aliases/marker.exs      |
      | Bash       | aliases/marker.sh       |
      | Bash       | aliases/marker.bash     |
      | Bash       | aliases/marker.bats     |

    Examples: grammar-backed exact filenames
      | language | path                   |
      | Ruby     | aliases/Vagrantfile    |
      | Ruby     | aliases/Rakefile       |
      | Bash     | aliases/.bashrc        |
      | Bash     | aliases/.bash_profile  |
      | Bash     | aliases/.bash_aliases  |
      | Bash     | aliases/.profile       |

    Examples: literal fallback exact filenames
      | language | path                   |
      | Docker   | aliases/Dockerfile     |
      | Docker   | aliases/Containerfile  |
      | Make     | aliases/Makefile       |
      | Make     | aliases/GNUmakefile    |
