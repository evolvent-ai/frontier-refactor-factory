# Serve a Ruby subject over the wire. Written into the task; not required by the factory.
#
# The subject supplies the function named on the command line, in subject.rb, raising to refuse; its
# parameters are the wire's `args`, splatted. Everything else is this file.

require 'json'

require_relative 'subject'

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
unless respond_to?(ENTRY, true)
  $stderr.puts "serve: subject.rb defines no method #{ENTRY}"
  exit 1
end

# The class and the message, never the backtrace: a backtrace carries absolute paths from the
# machine that produced it, and those would be frozen into an expectation nothing else can match.
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

  if request.fetch('op', 'run') == 'time'
    # TIMED HERE, on this side of the pipe. Measuring from the factory would charge the subject for
    # process startup and for JSON transport, which for a quick subject is most of the clock.
    repeats = request.fetch('repeats', 1).to_i
    failure = nil
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    repeats.times do
      begin
        send(ENTRY, *args)
      rescue StandardError => e
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
  rescue StandardError => e
    # A REFUSAL IS AN ANSWER. How the subject rejects bad input is behaviour a reimplementation has
    # to reproduce, so it is reported rather than raised, and the loop carries on reading.
    #
    # StandardError and not Exception: Interrupt, SignalException and NoMemoryError are the harness
    # being stopped or the machine running out, neither of which is an answer the subject gave.
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
