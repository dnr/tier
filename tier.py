#!/usr/bin/python

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
      out += 'N'
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
  assert t.endswith('/')
  if not relpaths:
    relpaths = ['']
  for relpath in relpaths:
    for path, dirs, fns in os.walk(join(t, relpath)):
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
    pass

  def __str__(self):
    return 'Abstract op'

  def __repr__(self):
    return 'Op()'

  def Run(self):
    raise NotImplementedError


class Symlink(Op):
  def __init__(self, dest, contents, was):
    self.dest = dest
    self.contents = contents
    self.was = was

  def __str__(self):
    return 'Symlink %s -> %s (was %s)' % (
        self.dest, self.contents, self.was)

  def __repr__(self):
    return 'Symlink(%r, %r, %r)' % (
        self.dest, self.contents, self.was)

  def Run(self):
    MakeBackupLink(self.dest)
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


class Copy(Op):
  def __init__(self, src, dest, was):
    self.src = src
    self.dest = dest
    self.was = was

  def __str__(self):
    return 'Copy %s -> %s (was %s)' % (
        self.src, self.dest, self.was)

  def __repr__(self):
    return 'Copy(%r, %r, %r)' % (
        self.src, self.dest, self.was)

  def Run(self):
    MakeBackupLink(self.dest)
    tmp = self.dest + '.tmp'
    try:
      shutil.copy2(self.src, tmp)
    except OSError, e:
      if e.errno == 2:
        os.makedirs(os.path.dirname(tmp))
        shutil.copy2(self.src, tmp)
      else:
        raise
    os.rename(tmp, self.dest)


class MissingFile(Op):
  def __init__(self, relpath, tps):
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

  def CheckConsistency(self, args, go):
    tcount = len(self.tiers)
    full = self.FullMap(args)
    full = full.items()
    full.sort()
    for relpath, bits in full:
      IT = lambda t: self.tiers[t] + relpath
      tps = UnpackBits(bits, tcount)
      ops = []
      # Find first file (most accessible copy). Above this should be links to
      # this, below this should be identical copies of this.
      ff = tps.find('F')
      if ff < 0:
        ops.append(MissingFile(relpath, tps))
      else:
        # These should be links to the file at ff.
        for i in xrange(0, ff):
          if tps[i] != 'L':
            ops.append(Symlink(IT(i), IT(ff), tps[i]))
          else:
            target = os.readlink(IT(i))
            if target != IT(ff):
              ops.append(Symlink(IT(i), IT(ff), 'L to %r' % target))
        # These should be files. Pick the one with the highest mtime and copy to
        # the rest.
        # {(mtime, size): [index]}
        data_candidates = collections.defaultdict(list)
        for i in xrange(ff, tcount):
          if tps[i] != 'F':
            ops.append(Copy(IT(ff), IT(i), tps[i]))
          else:
            ts = TimeAndSize(IT(i))
            data_candidates[ts].append(i)
        if len(data_candidates) > 1:
          data = data_candidates.items()
          data.sort(reverse=True)  # highest mtime first
          frm = min(data[0][1])  # pick highest tier out of those
          for _, indexes in data[1:]:
            for i in indexes:
              ops.append(Copy(IT(frm), IT(i), 'FIXME'))

      if ops:
        print '%s: %s' % (repr(relpath)[1:-1], tps)
        for op in ops:
          print '  ', op
          if go:
            op.Run()

  def List(self, args):
    tcount = len(self.tiers)
    full = self.FullMap(args)
    full = full.items()
    full.sort()
    for relpath, bits in full:
      tps = UnpackBits(bits, tcount)
      c = None
      ff = tps.find('F')
      if ff < 0:
        c = '?'
      else:
        c = str(ff)
      print c, relpath

  def Stats(self, args):
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
    print fmt % ('tier', 'files', 'cm files', 'size (MB)', 'cm size')
    for t in range(tcount):
      cfiles = sum(files[0:t+1])
      csize = sum(sizes[0:t+1])
      print fmt % (self.tiers[t], files[t], cfiles,
                   sizes[t] // MB, csize // MB)
    if files[-1]:
      print 'missing', files[-1]


def main(argv):
  parser = optparse.OptionParser()
  parser.add_option('-g', '--go', dest='go',
                    action='store_true', default=False)

  opts, args = parser.parse_args()

  config = open(TIER_CONFIG).read()
  tier = TierManager(config)

  if not args:
    print 'Missing command'
    return 1

  cmd = args.pop(0)
  if cmd == 'check':
    tier.CheckConsistency(args, opts.go)
  elif cmd == 'ls':
    tier.List(args)
  elif cmd == 'stats':
    tier.Stats(args)
  else:
    print 'Unknown command %r' % cmd


if __name__ == '__main__':
  sys.exit(main(sys.argv))
