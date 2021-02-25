import logging

def makeLogger(name):
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    # ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s][%(name)s][%(levelname)s] %(message)s')
    ch.setFormatter(formatter)
    log.addHandler(ch)

    return log
