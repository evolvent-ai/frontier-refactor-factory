# Serve a Ruby subject over the wire. Written into the task; not required by the factory.
#
# The subject supplies the function named on the command line, in subject.rb, raising to refuse; its
# parameters are the wire's `args`, splatted. Everything else is this file.

require 'json'

# THE GEM'S OWN LIB BELONGS ON THE LOAD PATH, for the same reason an `require` is. A ruby package
# subject is `subject.rb` sitting beside `lib/<name>.rb`, and `require 'delaunator'` searches
# $LOAD_PATH -- which by default is the system's directories only. So a library with NO dependencies
# at all, whose file is right there, fails with `cannot load such file`, and the failure reads
# identically to a missing gem. Measured: the single biggest class of package ruby freezes.
#
# THE GEM'S LIB BELONGS ON THE LOAD PATH, for the same reason an `require` is. A ruby package
# subject is `subject.rb` sitting beside the gem's entry file. Gems come in two layout conventions:
# `lib/<name>.rb` under a `lib/` directory, and `<name>.rb` directly at the package root. `require`
# searches $LOAD_PATH -- which by default is the system's directories only -- so without adding either
# location, a library whose file is right there fails with `cannot load such file`, and the failure
# reads identically to a missing gem. Measured: the single biggest class of package ruby freezes.
#
# `lib` is where rubygems keeps the entry file under its conventional layout; `__dir__` covers the
# root-entry convention (the `geometry` package has `geometry.rb` at the root, no `lib/`).
$LOAD_PATH.unshift(File.expand_path('lib', __dir__)) if File.directory?(File.expand_path('lib', __dir__))
$LOAD_PATH.unshift(__dir__)

# LOADING THE SUBJECT IS ITSELF A CALL THAT CAN FAIL, and this used to be a bare `require_relative`
# at the top of the file. A package whose gems are not installed raises `LoadError` from its own
# first `require`, before any rescue in this file exists, so the process died before writing a single
# reply and the factory recorded `the subject exited without answering` -- indistinguishable from a
# subject that hung, and charged to the material rather than to the environment.
#
# Measured: 17 of one package batch's freeze refusals, every one ruby, every one this.
#
# The failure is REMEMBERED rather than raised, so the wire still gets one reply per probe saying
# what went wrong. That is the same contract as a subject that raises when called: a refusal is an
# answer, and an answer is what freeze needs in order to say anything at all.
LOAD_FAULT = begin
  require_relative 'subject'
  nil
rescue ScriptError, StandardError => e
  e
end

# WHICH FUNCTION, from the command line -- exactly as serve.py takes it. This file used to call a
# method literally named `entry`, so it could only serve a subject somebody had written for the
# occasion; the material this factory mines is real code where the function is called `two_sum`.
# Being a dynamic language is not the same as binding a symbol: the splat was already general, the
# NAME was not, so a mined `two_sum` raised NameError and ruby needed no bridge to fix -- only this.
ENTRY = (ARGV[0] || 'entry').to_sym

# A top-level `def` in subject.rb becomes a private method on Object, and `send` reaches a private
# method where a plain call on an explicit receiver would not. Checked once, here, rather than per
# call: a missing entry point is our layout being wrong, not the subject refusing, and answering
# ok:false to every probe would freeze that mistake into an expectation as though it were behaviour.
unless LOAD_FAULT || respond_to?(ENTRY, true)
  $stderr.puts "serve: subject.rb defines no method #{ENTRY}"
  exit 1
end

# The class and the message, never the backtrace: a backtrace carries absolute paths from the
# machine that produced it, and those would be frozen into an expectation nothing else can match.
# WHAT COUNTS AS THE SUBJECT ANSWERING, as opposed to this harness being stopped.
#
# `StandardError` alone is too narrow, and the gap is not academic: Ruby puts `LoadError` and
# `NotImplementedError` under `ScriptError`, which descends from `Exception` directly and NOT from
# `StandardError`. A gem that cannot be required therefore raises straight through every
# `rescue StandardError` in this file, the shim dies before it can write a reply, and the factory
# records `the subject exited without answering` -- indistinguishable from a subject that hung.
# Measured: 17 of one package batch's freeze refusals, every one of them ruby, every one this.
#
# A failed `require` IS the subject's answer. It is what a submission would also hit, so it belongs
# in the corpus as a refusal rather than as a dead harness.
#
# Still excluded, and deliberately: `Interrupt` and `SignalException` are somebody stopping us,
# `NoMemoryError` and `SystemStackError` are the machine giving out. None of those is behaviour the
# subject chose, so they stay unrescued and end the process.
SUBJECT_FAULTS = [StandardError, ScriptError].freeze

def describe(error)
  "#{error.class}: #{error.message}"
end

def emit(reply)
  begin
    line = JSON.generate(reply)
  rescue StandardError => e
    # A value JSON cannot carry -- a cycle, an infinity, an object with no representation -- is a
    # failed call rather than a reason to stop reading.
    line = JSON.generate({ 'id' => reply['id'], 'ok' => false, 'error' => describe(e) })
  end
  $stdout.write(line << "\n")
  $stdout.flush
end

def handle(request)
  id = request['id']
  # AN ARGS FIELD OF THE WRONG SHAPE IS A REFUSAL, NOT AN EMPTY CALL. Treating both as `[]` is the
  # most expensive kind of wrong: `{"args": "not-a-list"}` was quietly rewritten into a call with no
  # arguments, the subject answered whatever a no-argument call returns, and the reply said ok:true.
  # A false SUCCESS enters grading as though it were a real answer.
  #
  # A MISSING field keeps the empty default: the factory always sends `args`, so its absence is a
  # hand-written line rather than a malformed call.
  if request.key?('args') && !request['args'].is_a?(Array)
    return { 'id' => id, 'ok' => false, 'error' => 'the arguments must be a JSON array' }
  end

  args = request['args'].is_a?(Array) ? request['args'] : []

  # THE SUBJECT NEVER LOADED, so every probe is refused with the reason -- once per probe, on the
  # wire, rather than once as a dead process. Checked here and not at startup because the wire's
  # contract is one reply per request, and a startup exit produces none.
  return { 'id' => id, 'ok' => false, 'error' => describe(LOAD_FAULT) } if LOAD_FAULT

  if request.fetch('op', 'run') == 'time'
    # TIMED HERE, on this side of the pipe. Measuring from the factory would charge the subject for
    # process startup and for JSON transport, which for a quick subject is most of the clock.
    repeats = request.fetch('repeats', 1).to_i
    failure = nil
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    repeats.times do
      begin
        send(ENTRY, *args)
      rescue *SUBJECT_FAULTS => e
        failure = e
        break
      end
    end
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    return failure.nil? ? { 'id' => id, 'ok' => true, 'seconds' => elapsed }
                        : { 'id' => id, 'ok' => false, 'error' => describe(failure) }
  end

  begin
    # Splatted: `args` is the argument LIST, so a two-parameter subject receives two arguments. See
    # the contract in protocol.py. `send` because a top-level def is a private method on Object.
    value = send(ENTRY, *args)
  rescue *SUBJECT_FAULTS => e
    # A REFUSAL IS AN ANSWER. How the subject rejects bad input is behaviour a reimplementation has
    # to reproduce, so it is reported rather than raised, and the loop carries on reading.
    #
    # See SUBJECT_FAULTS for which exceptions are the subject's answer and which are the harness
    # being stopped -- a failed `require` is the former, and used to escape as the latter.
    return { 'id' => id, 'ok' => false, 'error' => describe(e) }
  end

  { 'id' => id, 'ok' => true, 'value' => value }
end

begin
  # IO#each_line splits on newlines whatever size the pipe happened to deliver, so a probe larger
  # than one read is reassembled here rather than by hand.
  $stdin.each_line do |line|
    line = line.strip
    next if line.empty?

    begin
      request = JSON.parse(line)
    rescue JSON::ParserError
      next                                        # an unreadable line is not a call
    end
    next unless request.is_a?(Hash)

    emit(handle(request))
  end
rescue Errno::EPIPE
  # The factory closing the pipe first is not this process failing.
  exit 0
end
