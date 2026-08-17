# Serve a Ruby subject over the wire. Written into the task; not required by the factory.
#
# The subject supplies `entry(args)` in subject.rb, raising to refuse. Everything else is this file.

require 'json'

require_relative 'subject'

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
  args = request['args'].is_a?(Array) ? request['args'] : []

  if request.fetch('op', 'run') == 'time'
    # TIMED HERE, on this side of the pipe. Measuring from the factory would charge the subject for
    # process startup and for JSON transport, which for a quick subject is most of the clock.
    repeats = request.fetch('repeats', 1).to_i
    failure = nil
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    repeats.times do
      begin
        entry(args)
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
    value = entry(args)
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
