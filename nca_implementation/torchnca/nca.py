import numpy as np
import torch
import torch.nn as nn


class NCANonlinear:
  """Neighbourhood Components Analysis [1].

  References:
    [1]: https://www.cs.toronto.edu/~hinton/absps/nca.pdf
  """
  def __init__(self, dim=None, init="random", max_iters=500, tol=1e-5):
    """Constructor.

    Args:
      dim (int): The dimension of the the learned
        linear map A. If no dimension is provided,
        we assume a square matrix A of same dimension
        as the input data. For small values of `dim`
        (i.e. 2, 3), NCA will perform dimensionality
        reduction.
      init (str): The type of initialization to use for
        the matrix A.
          - `random`: A = N(0, I)
          - `identity`: A = I
      max_iters (int): The maximum number of iterations
        to run the optimization for.
      tol (float): The tolerance for convergence. If the
        difference between consecutive solutions is within
        this number, optimization is terminated.
    """
    self.dim = dim
    self.init = init
    self.max_iters = max_iters
    self.tol = tol
    self._mean = None
    self._stddev = None
    self._losses = None

  def __call__(self, X):
    """Apply the learned linear map to the input.
    """
    if self._mean is not None and self._stddev is not None:
      X = (X - self._mean) / self._stddev
    return torch.mm(self.feature_extractor(X), torch.t(self.A))

  def _init_transformation(self):
    """Initialize the linear transformation A.
    """
    if self.dim is None:
      self.dim = self.num_dims
    if self.init == "random":
      print('using random init')
      a = torch.randn(self.dim, self.num_dims, device=self.device) * 0.01
      self.A = torch.nn.Parameter(a)
    elif self.init == "identity":
      a = torch.eye(self.dim, self.num_dims, device=self.device)
      self.A = torch.nn.Parameter(a)
    else:
      raise ValueError("[!] {} initialization is not supported.".format(self.init))
    
    hidden_dims = [self.num_dims + 10, self.num_dims]
    layers = []
    prev_dim = self.num_dims

    for hidden_dim in hidden_dims:
      layers.append(nn.Linear(prev_dim, hidden_dim))
      layers.append(nn.Sigmoid())
      prev_dim = hidden_dim

    self.feature_extractor = nn.Sequential(*layers)

  @staticmethod
  def _pairwise_l2_sq(x):
    """Compute pairwise squared Euclidean distances.
    """
    dot = torch.mm(x.double(), torch.t(x.double()))
    norm_sq = torch.diag(dot)
    dist = norm_sq[None, :] - 2*dot + norm_sq[:, None]
    dist = torch.clamp(dist, min=0)  # replace negative values with 0
    return dist.float()

  @staticmethod
  def _softmax(x):
    """Compute row-wise softmax.

    Notes:
      Since the input to this softmax is the negative of the
      pairwise L2 distances, we don't need to do the classical
      numerical stability trick.
    """
    exp = torch.exp(x)

    sums = exp.sum(dim=1)
    sums += 0.01

    return exp / exp.sum(dim=1)

  @property
  def mean(self):
    if self._mean is None:
      raise ValueError('No mean was computed. Make sure normalize is set to True.')
    return self._mean

  @property
  def stddev(self):
    if self._stddev is None:
      raise ValueError('No stddev was computed. Make sure normalize is set to True.')
    return self._stddev

  @property
  def losses(self):
    if self._losses is None:
      raise ValueError('There are no losses to report. You must call train first.')
    return self._losses

  def loss(self, X, y_mask):
    # compute pairwise squared Euclidean distances
    # in transformed space
    embedding = torch.mm(self.feature_extractor(X), torch.t(self.A))
    distances = self._pairwise_l2_sq(embedding)

    # fill diagonal values such that exponentiating them
    # makes them equal to 0
    distances.diagonal().copy_(np.inf*torch.ones(len(distances)))

    # compute pairwise probability matrix p_ij
    # defined by a softmax over negative squared
    # distances in the transformed space.
    # since we are dealing with negative values
    # with the largest value being 0, we need
    # not worry about numerical instabilities
    # in the softmax function
    p_ij = self._softmax(-distances)

    # for each p_i, zero out any p_ij that
    # is not of the same class label as i
    p_ij_mask = p_ij * y_mask.float()

    # sum over js to compute p_i
    p_i = p_ij_mask.sum(dim=1)

    # compute expected number of points
    # correctly classified by summing
    # over all p_i's.
    # to maximize the above expectation
    # we can negate it and feed it to
    # a minimizer
    # for numerical stability, we only
    # log_sum over non-zero values
    classification_loss = -torch.log(torch.masked_select(p_i, p_i != 0)).sum()

    # to prevent the embeddings of different
    # classes from collapsing to the same
    # point, we add a hinge loss penalty
    distances.diagonal().copy_(torch.zeros(len(distances)))
    margin_diff = (1 - distances) * (~y_mask).float()
    hinge_loss = torch.clamp(margin_diff, min=0).pow(2).sum(1).mean()

    # sum both loss terms and return
    loss = classification_loss + hinge_loss
    return loss

  def train(
    self,
    X,
    y,
    batch_size=None,
    lr=1e-4,
    momentum=0.9,
    weight_decay=10,
    normalize=True,
  ):
    """Trains NCA until convergence.

    Specifically, we maximize the expected number of points
    correctly classified under a stochastic selection rule.
    This rule is defined using a softmax over Euclidean distances
    in the transformed space.

    Args:
      X (torch.FloatTensor): The dataset of shape (N, D) where
        `D` is the dimension of the feature space and `N`
        is the number of training examples.
      y (torch.LongTensor): The class labels of shape (N,).
      batch_size (int): How many data samples to use in an SGD
        update step.
      lr (float): The learning rate.
      weight_decay (float): The strength of the L2 regularization
        on the learned transformation A.
      normalize (bool): Whether to whiten the input, i.e. to
        subtract the feature-wise mean and divide by the
        feature-wise standard deviation.
    """
    self._losses = []
    self.num_train, self.num_dims = X.shape
    self.device = torch.device("cuda" if X.is_cuda else "cpu")
    if batch_size is None:
      batch_size = self.num_train
    batch_size = min(batch_size, self.num_train)

    # initialize the linear transformation matrix A
    self._init_transformation()

    torch.nn.utils.clip_grad_norm_(self.A, max_norm=0.5)

    # zero-mean the input data
    if normalize:
      self._mean = X.mean(dim=0)
      self._stddev = X.std(dim=0)
      X = (X - self._mean) / self._stddev

    optim_args = {
      'lr': lr,
      'weight_decay': weight_decay,
      'momentum': momentum,
    }
    optimizer = torch.optim.SGD([self.A], **optim_args)
    iters_per_epoch = int(np.ceil(self.num_train / batch_size))
    i_global = 0
    for epoch in range(self.max_iters):
      rand_idxs = torch.randperm(len(y))  # shuffle dataset
      X = X[rand_idxs]
      y = y[rand_idxs]
      # A_prev = optimizer.param_groups[0]['params'][0].clone()
      prev_params = [x.clone() for x in optimizer.param_groups[0]['params']]
      for i in range(iters_per_epoch):
        # grab batch
        X_batch = X[i*batch_size:(i+1)*batch_size]
        y_batch = y[i*batch_size:(i+1)*batch_size]

        # compute pairwise boolean class matrix
        y_mask = y_batch[:, None] == y_batch[None, :]

        # compute loss and take gradient step
        optimizer.zero_grad()
        loss = self.loss(X_batch, y_mask)
        self._losses.append(loss.item())
        loss.backward()
        torch.nn.utils.clip_grad_norm_([*self.feature_extractor.parameters(), self.A], max_norm=0.5)
        optimizer.step()

        i_global += 1
        if not i_global % 25:
          print("epoch: {} - loss: {:.5f}".format(epoch+1, loss.item()))

      # check if within convergence
      # A_curr = optimizer.param_groups[0]['params'][0]
      curr_params = optimizer.param_groups[0]['params']
      within_convergence = True

      for prev, cur in zip(prev_params, curr_params):
        if not torch.all(torch.abs(prev - cur) <= self.tol):
          within_convergence = False
          break

      # if torch.all(torch.abs(A_prev - A_curr) <= self.tol):
      if within_convergence:
        print("[*] Optimization has converged in {} mini batch iterations.".format(i_global))
        break

class NCALinear:
  """Neighbourhood Components Analysis [1].

  References:
    [1]: https://www.cs.toronto.edu/~hinton/absps/nca.pdf
  """
  def __init__(self, dim=None, init="random", max_iters=500, tol=1e-5):
    """Constructor.

    Args:
      dim (int): The dimension of the the learned
        linear map A. If no dimension is provided,
        we assume a square matrix A of same dimension
        as the input data. For small values of `dim`
        (i.e. 2, 3), NCA will perform dimensionality
        reduction.
      init (str): The type of initialization to use for
        the matrix A.
          - `random`: A = N(0, I)
          - `identity`: A = I
      max_iters (int): The maximum number of iterations
        to run the optimization for.
      tol (float): The tolerance for convergence. If the
        difference between consecutive solutions is within
        this number, optimization is terminated.
    """
    self.dim = dim
    self.init = init
    self.max_iters = max_iters
    self.tol = tol
    self._mean = None
    self._stddev = None
    self._losses = None

  def __call__(self, X):
    """Apply the learned linear map to the input.
    """
    if self._mean is not None and self._stddev is not None:
      X = (X - self._mean) / self._stddev
    return torch.mm(X, torch.t(self.A))

  def _init_transformation(self):
    """Initialize the linear transformation A.
    """
    if self.dim is None:
      self.dim = self.num_dims
    if self.init == "random":
      print('using random init')
      a = torch.randn(self.dim, self.num_dims, device=self.device) * 0.01
      self.A = torch.nn.Parameter(a)
    elif self.init == "identity":
      a = torch.eye(self.dim, self.num_dims, device=self.device)
      self.A = torch.nn.Parameter(a)
    else:
      raise ValueError("[!] {} initialization is not supported.".format(self.init))

  @staticmethod
  def _pairwise_l2_sq(x):
    """Compute pairwise squared Euclidean distances.
    """
    dot = torch.mm(x.double(), torch.t(x.double()))
    norm_sq = torch.diag(dot)
    dist = norm_sq[None, :] - 2*dot + norm_sq[:, None]
    dist = torch.clamp(dist, min=0)  # replace negative values with 0
    return dist.float()

  @staticmethod
  def _softmax(x):
    """Compute row-wise softmax.

    Notes:
      Since the input to this softmax is the negative of the
      pairwise L2 distances, we don't need to do the classical
      numerical stability trick.
    """
    exp = torch.exp(x)
    if torch.any(torch.isnan(exp)):
      print("---- NAN detected in _softmax() method ----")
      print("x: ")
      print(x)
      print("x shape:", x.shape)
      print("--------")
      print("exp: ")
      print(exp)
      print("exp shape:", exp.shape)
      print("--------")
      raise Exception("Manual Exception")
    
    sums = exp.sum(dim=1)
    sums += 0.01

    if not sums.all():
      print("---- 0 denominator detected in _softmax() method ----")

      for i in range(len(sums)):
        print("sum[" + str(i) + "] = " + str(sums[i]))

      raise Exception("Manual Exception")

    return exp / sums

  @property
  def mean(self):
    if self._mean is None:
      raise ValueError('No mean was computed. Make sure normalize is set to True.')
    return self._mean

  @property
  def stddev(self):
    if self._stddev is None:
      raise ValueError('No stddev was computed. Make sure normalize is set to True.')
    return self._stddev

  @property
  def losses(self):
    if self._losses is None:
      raise ValueError('There are no losses to report. You must call train first.')
    return self._losses

  def loss(self, X, y_mask):
    # compute pairwise squared Euclidean distances
    # in transformed space
    embedding = torch.mm(X, torch.t(self.A))

    if torch.any(torch.isnan(embedding)):
        print("---- NAN detected in _loss() method inside 'embedding' variable ----")
        print("X: ")
        print(X)
        print("X shape:", X.shape)
        print("--------")
        print("A: ")
        print(self.A)
        print("A shape:", self.A.shape)
        print("--------")
        print("embedding: ")
        print(embedding)
        print("embedding shape:", embedding.shape)
        print("--------")
        raise Exception("Manual Exception")

    distances = self._pairwise_l2_sq(embedding)

    if torch.any(torch.isnan(distances)):
      print("---- NAN detected in _loss() method inside 'distances' variable ----")
      print("embedding: ")
      print(embedding)
      print("embedding shape:", embedding.shape)
      print("--------")
      print("distances: ")
      print(distances)
      print("distances shape:", distances.shape)
      print("--------")
      raise Exception("Manual Exception")

    # fill diagonal values such that exponentiating them
    # makes them equal to 0
    distances.diagonal().copy_(np.inf*torch.ones(len(distances)))

    # compute pairwise probability matrix p_ij
    # defined by a softmax over negative squared
    # distances in the transformed space.
    # since we are dealing with negative values
    # with the largest value being 0, we need
    # not worry about numerical instabilities
    # in the softmax function
    p_ij = self._softmax(-distances)

    if torch.any(torch.isnan(p_ij)):
      print("---- NAN detected in _loss() method inside 'p_ij' variable ----")
      print("-distances: ")
      print(-distances)
      print("-distances shape:", (-distances).shape)
      print("--------")
      print("p_ij: ")
      print(p_ij)
      print("p_ij shape:", p_ij.shape)
      print("--------")
      dummy_1, dummy_2 = p_ij.shape[0], p_ij.shape[1]

      for i in range(dummy_1):
        for j in range(dummy_2):
          if torch.isnan(p_ij[i][j]):
            print("i: " + str(i) + ", j: " + str(j))
            print("-distances[i][j]:", (-distances)[i][j])

      raise Exception("Manual Exception")

    if torch.any(torch.isnan(y_mask.float())):
      print("---- NAN detected in _loss() method inside y_mask.float()")
      raise Exception("Manual Exception")

    # for each p_i, zero out any p_ij that
    # is not of the same class label as i
    p_ij_mask = p_ij * y_mask.float()

    if self.A.grad and torch.any(torch.isnan(self.A.grad)):
      raise Exception("Yo")

    if torch.any(torch.isnan(p_ij_mask)):
      print("---- NAN detected in _loss() method inside 'p_ij_mask' variable ----")
      raise Exception("Manual Exception")

    # sum over js to compute p_i
    p_i = p_ij_mask.sum(dim=1)

    if torch.any(torch.isnan(p_i)):
      print("---- NAN detected in _loss() method inside 'p_i' variable ----")
      raise Exception("Manual Exception")

    # compute expected number of points
    # correctly classified by summing
    # over all p_i's.
    # to maximize the above expectation
    # we can negate it and feed it to
    # a minimizer
    # for numerical stability, we only
    # log_sum over non-zero values
    classification_loss = -torch.log(torch.masked_select(p_i, p_i != 0)).sum()

    if torch.any(torch.isnan(classification_loss)):
      print("---- NAN detected in _loss() method inside 'classification_loss' variable ----")
      raise Exception("Manual Exception")   

    # to prevent the embeddings of different
    # classes from collapsing to the same
    # point, we add a hinge loss penalty
    distances.diagonal().copy_(torch.zeros(len(distances)))
    margin_diff = (1 - distances) * (~y_mask).float()

    if torch.any(torch.isnan(margin_diff)):
      print("---- NAN detected in _loss() method inside 'margin_diff' variable ----")
      raise Exception("Manual Exception") 

    hinge_loss = torch.clamp(margin_diff, min=0).pow(2).sum(1).mean()

    if torch.any(torch.isnan(hinge_loss)):
      print("---- NAN detected in _loss() method inside 'hinge_loss' variable ----")
      raise Exception("Manual Exception") 

    # sum both loss terms and return
    loss = classification_loss + hinge_loss

    if torch.any(torch.isnan(loss)):
      print("---- NAN detected in _loss() method inside 'loss' variable ----")
      raise Exception("Manual Exception")
    
    return loss, classification_loss, hinge_loss

  def train(
    self,
    X,
    y,
    batch_size=None,
    lr=1e-4,
    momentum=0.9,
    weight_decay=10,
    normalize=True,
  ):
    """Trains NCA until convergence.

    Specifically, we maximize the expected number of points
    correctly classified under a stochastic selection rule.
    This rule is defined using a softmax over Euclidean distances
    in the transformed space.

    Args:
      X (torch.FloatTensor): The dataset of shape (N, D) where
        `D` is the dimension of the feature space and `N`
        is the number of training examples.
      y (torch.LongTensor): The class labels of shape (N,).
      batch_size (int): How many data samples to use in an SGD
        update step.
      lr (float): The learning rate.
      weight_decay (float): The strength of the L2 regularization
        on the learned transformation A.
      normalize (bool): Whether to whiten the input, i.e. to
        subtract the feature-wise mean and divide by the
        feature-wise standard deviation.
    """
    self._losses = []
    self.num_train, self.num_dims = X.shape
    self.device = torch.device("cuda" if X.is_cuda else "cpu")
    if batch_size is None:
      batch_size = self.num_train
    batch_size = min(batch_size, self.num_train)

    # initialize the linear transformation matrix A
    self._init_transformation()

    # clip gradient norms
    torch.nn.utils.clip_grad_norm_(self.A, max_norm=0.5)

    # zero-mean the input data
    if normalize:
      self._mean = X.mean(dim=0)
      self._stddev = X.std(dim=0)
      X = (X - self._mean) / self._stddev

    optim_args = {
      'lr': lr,
      'weight_decay': weight_decay,
      'momentum': momentum,
    }
    optimizer = torch.optim.SGD([self.A], **optim_args)
    iters_per_epoch = int(np.ceil(self.num_train / batch_size))
    i_global = 0
    for epoch in range(self.max_iters):
      rand_idxs = torch.randperm(len(y))  # shuffle dataset
      X = X[rand_idxs]
      y = y[rand_idxs]
      A_prev = optimizer.param_groups[0]['params'][0].clone()
      for i in range(iters_per_epoch):
        # grab batch
        X_batch = X[i*batch_size:(i+1)*batch_size]
        y_batch = y[i*batch_size:(i+1)*batch_size]

        if (torch.any(torch.isnan(X_batch))):
          print("---- NAN detected in _train() method inside 'X_batch' variable")
          raise Exception("Manual Exception")
        
        if (torch.any(torch.isnan(y_batch))):
          print("---- NAN detected in _train() method inside 'y_batch' variable")
          raise Exception("Manual Exception")

        # compute pairwise boolean class matrix
        y_mask = y_batch[:, None] == y_batch[None, :]

        if torch.any(torch.isnan(y_mask)):
          print("---- NAN detected in _train() method inside 'y_mask' variable")
          raise Exception("Manual Exception")

        # compute loss and take gradient step
        optimizer.zero_grad()

        A_curr = optimizer.param_groups[0]['params'][0]
        if torch.any(torch.isnan(A_curr)):
          print("---- NAN detected in _train() method inside 'A_curr' variable after optimizer.zero_grad()")
          raise Exception('Manual Exception')

        loss, classif_loss, hinge_loss = self.loss(X_batch, y_mask)

        A_curr = optimizer.param_groups[0]['params'][0]
        if torch.any(torch.isnan(A_curr)):
          print("---- NAN detected in _train() method inside 'A_curr' variable after self.loss()")
          raise Exception('Manual Exception')

        self._losses.append(loss.item())

        A_curr = optimizer.param_groups[0]['params'][0]
        if torch.any(torch.isnan(A_curr)):
          print("---- NAN detected in _train() method inside 'A_curr' variable after self._losses.append()")
          raise Exception('Manual Exception')

        loss.backward()
        # clip gradient norms
        torch.nn.utils.clip_grad_norm_(self.A, max_norm=0.5)

        if torch.any(torch.isnan(self.A.grad)):
          print("---- NAN detected in _train() method inside 'self.A.grad' variable after loss.backward()")
          raise Exception("Manual Exception")

        A_curr = optimizer.param_groups[0]['params'][0]
        if torch.any(torch.isnan(A_curr)):
          print("---- NAN detected in _train() method inside 'A_curr' variable after loss.backward()")
          raise Exception('Manual Exception')
        
        optimizer.step()

        A_curr = optimizer.param_groups[0]['params'][0]
        if torch.any(torch.isnan(A_curr)):
          print("---- NAN detected in _train() method inside 'A_curr' variable after optimizer.step()")
          raise Exception('Manual Exception')

        i_global += 1
        if not i_global % 25:
          print("epoch: {} - loss: {:.5f}, classification loss: {:.5f}, hinge loss: {:.5f}".format(epoch+1, loss.item(), classif_loss.item(), hinge_loss.item()))
          # print("epoch: {} - loss: {:.5f}".format(epoch+1, loss.item()))

      # check if within convergence
      A_curr = optimizer.param_groups[0]['params'][0]
      if torch.all(torch.abs(A_prev - A_curr) <= self.tol):
        print("[*] Optimization has converged in {} mini batch iterations.".format(i_global))
        break