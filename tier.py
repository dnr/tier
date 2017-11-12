#!/usr/bin/python

__author__ = 'David Reiss <davidn@gmail.com>'

import sys, os, stat, shutil, shlex, hashlib, collections, optparse, time
join = os.path.join

TIER_CONFIG = join(os.getenv('HOME'), '.config', 'tier.conf')
TIER_IGNORE = '.tierignore'
TIER_BACKUP_INFIX = '.tierbk.'

NONE = 0
FILE = 1
LINK = 2
OTHER = 3

global_run_id = '%r.%r' % (time.time(), os.getpid())

def UnpackBits(bits, tiers):
  out = ''
  for t in range(tiers):
    tp = (bits & (3 << (t * 2))) >> (t * 2)
    if tp == NONE:
      out += '-'
    elif tp == FILE:
      out += 'F'
    elif tp == LINK:
      out += 'L'
    elif tp == OTHER:
      out += 'O'
  return out

def TimeAndSize(fn):
  st = os.lstat(fn)
  return int(st.st_mtime), st.st_size

def MakeBackupLink(fn):
  if not fn: return
  bk = fn + TIER_BACKUP_INFIX + global_run_id
  try:
    os.link(fn, bk)
  except OSError:
    pass

def GetType(fn):
  try:
    mode = os.lstat(fn).st_mode
    if stat.S_ISREG(mode):
      return FILE
    elif stat.S_ISLNK(mode):
      return LINK
    else:
      return OTHER
  except OSError:
    return NONE

def AllFilesInTree(t, relpaths):
  # TODO: this function is a mess. rewrite it.
  assert t.endswith('/')
  if not relpaths:
    relpaths = ['']
  for relpath in relpaths:
    fullpath = join(t, relpath)
    tp = GetType(fullpath)
    if ((tp == FILE or tp == LINK) and
        relpath != TIER_IGNORE and
        TIER_BACKUP_INFIX not in relpath):
      yield relpath
    else:
      for path, dirs, fns in os.walk(fullpath):
        if TIER_IGNORE in fns:
          dirs[:] = []
          continue
        assert path.startswith(t)
        path = path[len(t):]
        for fn in fns:
          if TIER_BACKUP_INFIX not in fn:
            fn = join(path, fn)
            yield fn

def Fileprint(fn):
  CHUNK = 1024
  f = open(fn)
  s = hashlib.sha1()
  s.update(f.read(CHUNK))
  f.seek(0, 2)
  l = f.tell()
  f.seek(l / 2 / CHUNK * CHUNK)
  s.update(f.read(CHUNK))
  f.seek(max(0, (l - CHUNK) / CHUNK * CHUNK))
  s.update(f.read(CHUNK))
  return s.digest()


class Op(object):
  def __init__(self):
    self.dest = None

  def __str__(self):
    return 'Abstract op'

  def __repr__(self):
    return 'Op()'

  def Run(self):
    raise NotImplementedError

  def Short(self):
    return '?'


class Symlink(Op):
  def __init__(self, di, dest, ci, contents, was):
    Op.__init__(self)
    self.di = di
    self.dest = dest
    self.ci = ci
    self.contents = contents
    self.was = was

  def __str__(self):
    return 'Symlink %s -> %s (was %s)' % (
        self.dest, self.contents, self.was)

  def __repr__(self):
    return 'Symlink(%r, %r, %r, %r, %r)' % (
        self.di, self.dest, self.ci, self.contents, self.was)

  def Run(self):
    tmp = self.dest + '.tmp'
    try:
      os.symlink(self.contents, tmp)
    except OSError, e:
      if e.errno == 2:
        os.makedirs(os.path.dirname(tmp))
        os.symlink(self.contents, tmp)
      else:
        raise
    os.rename(tmp, self.dest)

  def Short(self):
    return '%d -> %d' % (self.di + 1, self.ci + 1)  # ui is 1-based


class Copy(Op):
  def __init__(self, si, src, di, dest, was):
    Op.__init__(self)
    self.si = si
    self.src = src
    self.di = di
    self.dest = dest
    self.was = was

  def __str__(self):
    return 'Copy %s -> %s (was %s)' % (
        self.src, self.dest, self.was)

  def __repr__(self):
    return 'Copy(%r, %r, %r, %r, %r)' % (
        self.si, self.src, self.di, self.dest, self.was)

  def Run(self):
    tmp = self.dest + '.tmp'
    try:
      shutil.copy2(self.src, tmp)
    except (OSError, IOError), e:
      if e.errno == 2:
        os.makedirs(os.path.dirname(tmp))
        shutil.copy2(self.src, tmp)
      else:
        raise
    os.rename(tmp, self.dest)

  def Short(self):
    return '%d ==> %d' % (self.si + 1, self.di + 1)  # ui is 1-based


class MissingFile(Op):
  def __init__(self, relpath, tps):
    Op.__init__(self)
    self.relpath = relpath
    self.tps = tps

  def __str__(self):
    return 'Missing file at %s (%s)' % (self.relpath, self.tps)

  def __repr__(self):
    return 'MissingFile(%r, %r)' % (self.relpath, self.tps)


class TierManager(object):
  def __init__(self, config):
    # Paths in self.tiers end in /
    self.tiers = []
    self.LoadConfig(config)

  def LoadConfig(self, config):
    for line in config.splitlines():
      line = shlex.split(line, comments=True)
      if line and line[0] == 'tier':
        tier = line[1].rstrip('/') + '/'
        assert tier.startswith('/')
        self.tiers.append(tier)

  def WhichTier(self, path):
    """Returns: tuple of:
      tier index (-1 if not in any tier)
      relative path inside tier (original path if not in any tier)
    """
    path = os.path.realpath(path)
    for i, t in enumerate(self.tiers):
      if t == path + '/':
        return i, ''
      if path.startswith(t):
        return i, path[len(t):]
    return -1, path

  def InTier(self, t, relpath):
    return self.tiers[t] + relpath

  def FullMap(self, args):
    """Returns:
      map from relative filename to packed types
    """
    relpaths = []
    for limit in args:
      t, relpath = self.WhichTier(limit)
      assert t >= 0, 'Not in any tier: %s' % relpath
      relpaths.append(relpath)
    full = collections.defaultdict(int)
    for i, t in enumerate(self.tiers):
      for f in AllFilesInTree(t, relpaths):
        tp = GetType(join(t, f))
        full[f] |= (tp << (i * 2))
    return full

  def Sync(self, args, opts):
    tcount = len(self.tiers)
    full = self.FullMap(args)
    full = full.items()
    full.sort()
    for relpath, bits in full:
      IT = lambda t: self.tiers[t] + relpath
      tps = UnpackBits(bits, tcount)
      ops = []

      # Choose target first file.
      if opts.tier is None:
        tff = tps.find('F')
      else:
        tff = opts.tier - 1  # ui is 1-based
      assert 0 <= tff < tcount, 'tier out of range or missing file: ' + relpath

      # Find the most recent copy.
      # {(mtime, size): [index]}
      data_candidates = collections.defaultdict(list)
      for i in xrange(0, tcount):
        if tps[i] == 'F':
          ts = TimeAndSize(IT(i))
          data_candidates[ts].append(i)
      if not data_candidates:
        ops.append(MissingFile(relpath, tps))
      else:
        best = max(data_candidates)  # highest mtime
        bestindexes = data_candidates[best]
        frm = min(bestindexes)  # pick highest tier out of those
        # Copy to all tiers that should have a file, that don't have the
        # matching file.
        # TODO: when copying from 2 to 1 and 0, copy from 2 to 1, then 1 to 0,
        # instead of 2 to 1 and 2 to 0. will probably be faster if 2 is nfs.
        for i in xrange(tff, tcount):
          if i not in bestindexes:
            ops.append(Copy(frm, IT(frm), i, IT(i), tps[i]))

        # These should be links to the file at tff.
        # Note that the copying happens before the linking, in case we need to
        # copy a file to a lower tier and then replace it to a link at the same
        # time.
        for i in xrange(0, tff):
          if tps[i] != 'L':
            ops.append(Symlink(i, IT(i), tff, IT(tff), tps[i]))
          else:
            target = os.readlink(IT(i))
            if target != IT(tff):
              ops.append(Symlink(i, IT(i), tff, IT(tff), 'L to %r' % target))

      if ops:
        shortdesc = ', '.join(op.Short() for op in ops)
        print '%s  %s  %s' % (tps, repr(relpath)[1:-1], shortdesc)
        for op in ops:
          if opts.verbose:
            print '  ', op
          if opts.go:
            if opts.backup:
              MakeBackupLink(op.dest)
            op.Run()

  def List(self, args, opts):
    tcount = len(self.tiers)
    full = self.FullMap(args)
    full = full.items()
    full.sort()
    for relpath, bits in full:
      tps = UnpackBits(bits, tcount)
      t = None
      ff = tps.find('F')
      if ff < 0:
        t = '?'
      else:
        t = str(ff + 1)  # ui is 1-based
      print t, relpath

  def Stats(self, args, opts):
    tcount = len(self.tiers)
    files = [0] * (tcount + 1)
    sizes = [0] * tcount

    for relpath, bits in self.FullMap(args).iteritems():
      tps = UnpackBits(bits, tcount)
      ff = tps.find('F')
      files[ff] += 1
      if ff >= 0:
        sizes[ff] += TimeAndSize(self.InTier(ff, relpath))[1]

    fmt = '%-20s  %10s  %10s  %10s  %10s'
    MB = 1024 * 1024
    print fmt % ('tier', 'files', 'tot files', 'size (MB)', 'tot size')
    for t in range(tcount):
      tfiles = sum(files[0:t+1])
      tsize = sum(sizes[0:t+1])
      print fmt % (self.tiers[t].rstrip('/'), files[t], tfiles,
                   sizes[t] // MB, tsize // MB)
    if files[-1]:
      print 'missing', files[-1]

  def Exec(self, argv):
    cwd = os.getcwd()
    t, relpath = self.WhichTier(cwd)
    assert t >= 0, 'Not in any tier: %s' % relpath
    mret = 0
    for t in range(len(self.tiers)):
      path = self.InTier(t, relpath)
      print '====== in', path.rstrip('/')
      os.chdir(path)
      ret = os.spawnvp(os.P_WAIT, argv[0], argv)
      if ret:
        print '====== returned', ret
      mret = max(mret, ret)
    return mret


def PopArg(args, *cmds):
  if args and args[0] in cmds:
    args.pop(0)
    return True
  else:
    return False


def main():
  config = open(TIER_CONFIG).read()
  tier = TierManager(config)

  if len(sys.argv) > 1 and sys.argv[1] == 'exec':
    return tier.Exec(sys.argv[2:])
  elif len(sys.argv) > 1 and sys.argv[1] in ('mv', 'rm'):
    return tier.Exec(sys.argv[1:])

  parser = optparse.OptionParser()
  parser.add_option('-v', '--verbose', action='store_true')
  parser.add_option('-g', '--go', action='store_true')
  parser.add_option('-b', '--backup', action='store_true', default=True)
  parser.add_option('-n', '--no-backup', action='store_false', dest='backup')

  # Destination tier for sync.
  parser.add_option('-t', '--tier', type='int', default=None)
  parser.add_option('-1', const=1, dest='tier', action='store_const')
  parser.add_option('-2', const=2, dest='tier', action='store_const')
  parser.add_option('-3', const=3, dest='tier', action='store_const')
  parser.add_option('-4', const=4, dest='tier', action='store_const')

  opts, args = parser.parse_args()

  func = tier.Sync
  if PopArg(args, 'ls'):
    func = tier.List
  elif PopArg(args, 'stat', 'stats'):
    func = tier.Stats

  func(args, opts)


if __name__ == '__main__':
  sys.exit(main())
